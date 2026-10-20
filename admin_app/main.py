"""Danube Ops — standalone bank management app.

Runs independently from the customer app (default port 8001) and talks to the
same MySQL database through the shared SQLAlchemy models and banking services.
Every mutation is audited under the operator's account.
"""
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

from app.config import settings
from app.db import Base, SessionLocal, engine, get_db
from app.security import csrf_token
from app.services import settings as rules
from app.utils import template_ctx

from . import auth, banking_ops, system, views
from .deps import render, templates

BASE_DIR = Path(__file__).resolve().parent
APP_STATIC = BASE_DIR.parent / "app" / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(engine)
    with SessionLocal() as db:
        rules.ensure_defaults(db)
        db.commit()
        rules.warm_cache(db)
    yield


app = FastAPI(title="Danube Ops", docs_url="/api/docs", redoc_url=None, lifespan=lifespan)
app.state.templates = templates
app.state.render = render

app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
app.mount("/shared", StaticFiles(directory=APP_STATIC), name="shared")

_cache_state = {"at": 0.0}


@app.middleware("http")
async def session_setup(request: Request, call_next):
    request.state.user = None
    if "csrf" not in request.session:
        request.session["csrf"] = csrf_token()

    now = time.monotonic()
    if now - _cache_state["at"] > 10:
        try:
            with SessionLocal() as db:
                rules.warm_cache(db)
            _cache_state["at"] = now
        except Exception:  # pragma: no cover
            pass
    return await call_next(request)


app.add_middleware(
    SessionMiddleware,
    secret_key=f"{settings.secret_key}:admin",
    session_cookie="danube_admin_session",
    same_site="lax",
    https_only=False,
    max_age=60 * 60 * 4,
)

for module in (auth, views, banking_ops, system):
    app.include_router(module.router)


@app.get("/health")
def health(db: Session = Depends(get_db)):
    db.execute(text("SELECT 1"))
    return JSONResponse({"status": "ok", "app": "Danube Ops", "database": "connected"})


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
