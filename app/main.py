"""FastAPI application entrypoint for the customer-facing banking app."""
from __future__ import annotations

import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

from .config import settings
from .db import Base, SessionLocal, engine, get_db
from .deps import render, templates
from .routers import accounts, admin, auth, cards, loans, transfers
from .security import csrf_token
from .services import settings as rules
from .utils import template_ctx

BASE_DIR = Path(__file__).resolve().parent


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(engine)
    with SessionLocal() as db:
        rules.ensure_defaults(db)
        db.commit()
        rules.warm_cache(db)
    yield


app = FastAPI(title=settings.app_name, docs_url="/api/docs", redoc_url=None, lifespan=lifespan)
app.state.templates = templates
app.state.render = render

app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")

_cache_state = {"at": 0.0}
_MAINTENANCE_ALLOW = ("/health", "/static", "/login", "/maintenance")


@app.middleware("http")
async def session_setup(request: Request, call_next):
    request.state.user = None
    if "csrf" not in request.session:
        request.session["csrf"] = csrf_token()

    # Settings are edited from a separate admin process, so refresh the cache periodically.
    now = time.monotonic()
    if now - _cache_state["at"] > 10:
        try:
            with SessionLocal() as db:
                rules.warm_cache(db)
            _cache_state["at"] = now
        except Exception:  # pragma: no cover - DB not ready yet
            pass

    if rules.cached("ops.maintenance_mode", "false") == "true" and not request.url.path.startswith(_MAINTENANCE_ALLOW):
        return templates.TemplateResponse(
            request,
            "maintenance.html",
            template_ctx(request, title="Scheduled maintenance"),
            status_code=503,
        )
    return await call_next(request)


# Added last so it wraps the middleware above (last added = outermost).
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.secret_key,
    session_cookie=settings.session_cookie,
    same_site="lax",
    https_only=False,
    max_age=60 * 60 * 8,
)


for module in (auth, accounts, transfers, cards, loans, admin):
    app.include_router(module.router)


@app.get("/maintenance")
def maintenance_page(request: Request):
    return templates.TemplateResponse(
        request, "maintenance.html", template_ctx(request, title="Scheduled maintenance"), status_code=503
    )


@app.get("/health")
def health(db: Session = Depends(get_db)):
    db.execute(text("SELECT 1"))
    return JSONResponse(
        {
            "status": "ok",
            "app": rules.cached("bank.name", settings.app_name),
            "maintenance": rules.cached("ops.maintenance_mode", "false") == "true",
        }
    )


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    location = (exc.headers or {}).get("Location")
    if exc.status_code in (301, 302, 303, 307) and location:
        return RedirectResponse(location, status_code=303)
    if request.headers.get("accept", "").startswith("text/html"):
        return templates.TemplateResponse(
            request,
            "error.html",
            template_ctx(request, status_code=exc.status_code, message=exc.detail),
            status_code=exc.status_code,
        )
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
