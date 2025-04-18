import ctypes
import datetime as dt
import json
import os
import time
from pathlib import Path
from threading import Lock
from typing import Any, Callable, Dict, Optional

from babel.dates import format_timedelta
from dotenv import load_dotenv
from flask import Flask, current_app, g, request
from flask_babel import Babel, _
from flask_sock import Sock
from werkzeug.serving import is_running_from_reloader

from tronbyt_server import db, system_apps

babel = Babel()
sock = Sock()
pixlet_render_app: Optional[
    Callable[[bytes, bytes, int, int, int, int, int, int, int], Any]
] = None
pixlet_get_schema: Optional[Callable[[bytes], Any]] = None
pixlet_call_handler: Optional[
    Callable[[ctypes.c_char_p, ctypes.c_char_p, ctypes.c_char_p], Any]
] = None
pixlet_init_cache: Optional[Callable[[], None]] = None
pixlet_init_redis_cache: Optional[Callable[[bytes], None]] = None
pixlet_free_bytes: Optional[Callable[[Any], None]] = None


def load_pixlet_library() -> None:
    libpixlet_path = Path(os.getenv("LIBPIXLET_PATH", "/usr/lib/libpixlet.so"))
    current_app.logger.info(f"Loading {libpixlet_path}")
    try:
        pixlet_library = ctypes.cdll.LoadLibrary(str(libpixlet_path))
    except OSError as e:
        raise RuntimeError(f"Failed to load {libpixlet_path}: {e}")

    global pixlet_init_redis_cache
    pixlet_init_redis_cache = pixlet_library.init_redis_cache
    pixlet_init_redis_cache.argtypes = [ctypes.c_char_p]

    global pixlet_init_cache
    pixlet_init_cache = pixlet_library.init_cache

    global pixlet_render_app
    pixlet_render_app = pixlet_library.render_app
    pixlet_render_app.argtypes = [
        ctypes.c_char_p,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
    ]

    class DataReturn(ctypes.Structure):
        _fields_ = [
            ("data", ctypes.POINTER(ctypes.c_ubyte)),
            ("length", ctypes.c_int),
        ]

    class StringReturn(ctypes.Structure):
        _fields_ = [
            ("data", ctypes.c_char_p),
            ("length", ctypes.c_int),
        ]

    pixlet_render_app.restype = DataReturn

    global pixlet_get_schema
    pixlet_get_schema = pixlet_library.get_schema
    pixlet_get_schema.argtypes = [ctypes.c_char_p]
    pixlet_get_schema.restype = DataReturn

    global pixlet_call_handler
    pixlet_call_handler = pixlet_library.call_handler
    pixlet_call_handler.argtypes = [
        ctypes.c_char_p,
        ctypes.c_char_p,
        ctypes.c_char_p,
    ]
    pixlet_call_handler.restype = StringReturn

    global pixlet_free_bytes
    pixlet_free_bytes = pixlet_library.free_bytes
    pixlet_free_bytes.argtypes = [ctypes.c_void_p]


_pixlet_initialized = False
_pixlet_lock = Lock()


def initialize_pixlet_library() -> None:
    global _pixlet_initialized
    with _pixlet_lock:
        if _pixlet_initialized:
            return

        load_pixlet_library()

        redis_url = os.getenv("REDIS_URL")
        if redis_url and pixlet_init_redis_cache:
            current_app.logger.info(f"Using Redis cache at {redis_url}")
            pixlet_init_redis_cache(redis_url.encode("utf-8"))
        elif pixlet_init_cache:
            pixlet_init_cache()
        _pixlet_initialized = True


def render_app(
    path: Path,
    config: Dict[str, Any],
    width: int,
    height: int,
    magnify: int,
    maxDuration: int,
    timeout: int,
    image_format: int,
    silence_output: bool,
) -> Optional[bytes]:
    initialize_pixlet_library()
    if not pixlet_render_app:
        return None
    ret = pixlet_render_app(
        str(path).encode("utf-8"),
        json.dumps(config).encode("utf-8"),
        width,
        height,
        magnify,
        maxDuration,
        timeout,
        image_format,
        1 if silence_output else 0,
    )
    if ret.length >= 0:
        data = ctypes.cast(
            ret.data, ctypes.POINTER(ctypes.c_byte * ret.length)
        ).contents
        buf = bytes(data)
        if pixlet_free_bytes and ret.data:
            pixlet_free_bytes(ret.data)
        return buf
    return None


def get_schema(path: Path) -> Optional[str]:
    initialize_pixlet_library()
    if not pixlet_get_schema:
        return None
    ret = pixlet_get_schema(str(path).encode("utf-8"))
    if ret.length >= 0:
        data = ctypes.cast(
            ret.data, ctypes.POINTER(ctypes.c_byte * ret.length)
        ).contents
        try:
            buf = bytes(data).decode("utf-8")
        except UnicodeDecodeError as e:
            current_app.logger.error(f"UnicodeDecodeError: {e}")
            buf = None
        if pixlet_free_bytes and ret.data:
            pixlet_free_bytes(ret.data)
        return buf
    return None


def call_handler(path: Path, handler: str, parameter: str) -> Optional[str]:
    initialize_pixlet_library()
    if not pixlet_call_handler:
        return None
    ret = pixlet_call_handler(
        ctypes.c_char_p(str(path).encode("utf-8")),
        ctypes.c_char_p(handler.encode("utf-8")),
        ctypes.c_char_p(parameter.encode("utf-8")),
    )
    if ret.length >= 0:
        data = ctypes.string_at(ret.data)
        try:
            buf = data.decode("utf-8")
        except Exception as e:
            current_app.logger.error(f"Error: {e}")
            buf = None
        return buf
    return None


def get_locale() -> Optional[str]:
    return request.accept_languages.best_match(current_app.config["LANGUAGES"])


def create_app(test_config: Optional[Dict[str, Any]] = None) -> Flask:
    load_dotenv()

    # The reloader will run this code twice, once in the main process and once in the child process.
    # This is a workaround to avoid running the update_system_repo() function twice.
    if not is_running_from_reloader():
        system_apps.update_system_repo()

    # create and configure the app
    app = Flask(__name__, instance_relative_config=True)
    if test_config is None:
        app.config.from_mapping(
            SECRET_KEY="lksdj;as987q3908475ukjhfgklauy983475iuhdfkjghairutyh",
            MAX_CONTENT_LENGTH=1000 * 1000,  # 1mbyte upload size limit
            SERVER_HOSTNAME=os.getenv("SERVER_HOSTNAME", "localhost"),
            SERVER_PROTOCOL=os.getenv("SERVER_PROTOCOL", "http"),
            MAIN_PORT=os.getenv("SERVER_PORT", "8000"),
            USERS_DIR="users",
            PRODUCTION=os.getenv("PRODUCTION", "1"),
            DB_FILE="users/usersdb.sqlite",
            LANGUAGES=["en", "de"],
        )
        if app.config.get("PRODUCTION") == "1":
            if app.config["SERVER_PROTOCOL"] == "https":
                app.config["SESSION_COOKIE_SECURE"] = True
            app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
            app.logger.setLevel(os.getenv("LOG_LEVEL", "WARNING"))
        else:
            app.logger.setLevel(os.getenv("LOG_LEVEL", "INFO"))
    else:
        app.config.from_mapping(
            SECRET_KEY="lksdj;as987q3908475ukjhfgklauy983475iuhdfkjghairutyh",
            MAX_CONTENT_LENGTH=1000 * 1000,  # 1mbyte upload size limit
            SERVER_PROTOCOL=os.getenv("SERVER_PROTOCOL", "http"),
            DB_FILE="users/testdb.sqlite",
            LANGUAGES=["en"],
            SERVER_HOSTNAME="localhost",
            MAIN_PORT=os.getenv("SERVER_PORT", "8000"),
            USERS_DIR="tests/users",
            PRODUCTION="0",
            TESTING=True,
        )
    babel.init_app(app, locale_selector=get_locale)

    instance_path = Path(app.instance_path)
    try:
        instance_path.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass

    # Initialize the database within the application context
    with app.app_context():
        db.init_db()

    from . import auth

    app.register_blueprint(auth.bp)

    from . import api

    app.register_blueprint(api.bp)

    from . import manager

    app.register_blueprint(manager.bp)
    app.add_url_rule("/", endpoint="index")

    sock.init_app(app)

    @app.template_filter("timeago")
    def timeago(seconds: int) -> str:
        if seconds == 0:
            return str(_("Never"))
        return format_timedelta(
            dt.timedelta(seconds=seconds - int(time.time())),
            granularity="second",
            add_direction=True,
            locale=get_locale(),
        )

    @app.teardown_appcontext
    def close_connection(exception: Any) -> None:
        db = getattr(g, "_database", None)
        if db is not None:
            db.close()

    return app
