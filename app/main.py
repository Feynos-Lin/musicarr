"""Musicarr entry point: Starlette app, auth, static UI, background tasks."""
import asyncio
import base64
import os
import secrets
from contextlib import asynccontextmanager

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.gzip import GZipMiddleware
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from . import api, db, slskd_admin, tasks
from .util import VERSION, log, setup_logging

STATIC = os.path.join(os.path.dirname(__file__), "static")
# app.js/style.css are cached by the browser for a year; the version changes whenever they do
ASSET_VERSION = str(int(max(os.path.getmtime(os.path.join(STATIC, f)) for f in ("app.js", "style.css"))))


class CachedStatic(StaticFiles):
    async def get_response(self, path, scope):
        resp = await super().get_response(path, scope)
        if resp.status_code == 200 and b"v=" in scope.get("query_string", b""):
            resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return resp


class Auth(BaseHTTPMiddleware):
    """Optional HTTP Basic auth for the UI; API key (header X-Api-Key or ?apikey=) always works for the API."""

    async def dispatch(self, req, call_next):
        path = req.url.path
        if path in ("/ping",) or path.startswith("/static/") and path.endswith((".css", ".svg", ".png", ".webmanifest")):
            return await call_next(req)
        key = db.get_setting("api_key")
        given = req.headers.get("x-api-key") or req.query_params.get("apikey")
        if given and secrets.compare_digest(given, key):
            return await call_next(req)
        user, pw = db.get_setting("auth_username"), db.get_setting("auth_password")
        if not user:
            return await call_next(req)
        h = req.headers.get("authorization", "")
        if h.startswith("Basic "):
            try:
                u, p = base64.b64decode(h[6:]).decode().split(":", 1)
                if secrets.compare_digest(u, user) and secrets.compare_digest(p, pw):
                    return await call_next(req)
            except Exception:
                pass
        return Response("Authentication required", 401, {"WWW-Authenticate": 'Basic realm="Musicarr"'})


async def server_error(req, exc):
    import traceback
    log.error("Unhandled error on %s %s: %s\n%s", req.method, req.url.path, exc, traceback.format_exc())
    return JSONResponse({"error": f"{type(exc).__name__}: {exc}"}, status_code=500)


async def index(req):
    html = open(os.path.join(STATIC, "index.html")).read().replace("__V__", ASSET_VERSION)
    from starlette.responses import HTMLResponse
    return HTMLResponse(html, headers={"Cache-Control": "no-cache"})


async def ping(req):
    return JSONResponse({"status": "OK", "version": VERSION})


@asynccontextmanager
async def lifespan(app):
    setup_logging()
    db.init()
    slskd_admin.ensure_client()
    tasks.start(asyncio.get_running_loop())
    log.info("Musicarr %s started — config in %s", VERSION, db.CONFIG_DIR)
    yield
    log.info("Musicarr shutting down")
    try:
        with db._lock:
            db.conn().execute("PRAGMA wal_checkpoint(PASSIVE)")
    except Exception:
        pass
    # Background jobs may be mid-way through a file on a slow mount; exit now rather than waiting for them.
    import threading
    t = threading.Timer(2.0, os._exit, args=(0,))
    t.daemon = True
    t.start()


app = Starlette(
    routes=[Route("/", index), Route("/ping", ping), *api.routes,
            Mount("/static", CachedStatic(directory=STATIC), name="static")],
    middleware=[Middleware(GZipMiddleware, minimum_size=1024), Middleware(Auth)],
    lifespan=lifespan,
    exception_handlers={Exception: server_error},
)


def run():
    import uvicorn
    uvicorn.run("app.main:app", host="0.0.0.0", port=int(os.environ.get("MUSICARR_PORT", 8689)), log_level="warning")


if __name__ == "__main__":
    run()
