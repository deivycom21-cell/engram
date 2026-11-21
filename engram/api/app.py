"""FastAPI application factory: API + static frontend, hardened for local use."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from engram import __version__
from engram.api.routes import router
from engram.core.engine import EngramEngine

log = logging.getLogger("engram.api")
FRONTEND = Path(__file__).resolve().parents[1] / "frontend"

CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
    "connect-src 'self'; font-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
)


def create_app(engine: EngramEngine | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if getattr(app.state, "engine", None) is None:
            from engram.core.lifecycle import get_engine

            app.state.engine = get_engine()
        yield
        if app.state.owns_engine:
            app.state.engine.close()

    app = FastAPI(title="ENGRAM", version=__version__, lifespan=lifespan, docs_url="/api/docs", redoc_url=None,
                  openapi_url="/api/openapi.json")
    app.state.engine = engine
    app.state.owns_engine = engine is None

    hosts = ["127.0.0.1", "localhost", "testserver", "[::1]"]
    if engine is not None and engine.settings.host not in ("0.0.0.0", "::"):
        hosts.append(engine.settings.host)
    # Blocks DNS-rebinding: a malicious web page cannot reach the API through a foreign Host header.
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=hosts)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("Content-Security-Policy", CSP)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
        return response

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception):  # surface errors, never hide them
        log.exception("unhandled error on %s", request.url.path)
        return JSONResponse({"detail": f"{exc.__class__.__name__}: {exc}"}, status_code=500)

    app.include_router(router)
    app.mount("/", StaticFiles(directory=str(FRONTEND), html=True), name="frontend")
    return app
