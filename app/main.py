"""FastAPI application entrypoint."""
from __future__ import annotations

from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

from .config import settings
from .db import get_db
from .deps import render, templates
from .routers import accounts, admin, auth, cards, loans, transfers
from .security import csrf_token
from .utils import template_ctx

BASE_DIR = Path(__file__).resolve().parent

app = FastAPI(title=settings.app_name, docs_url="/api/docs", redoc_url=None)
app.state.templates = templates
app.state.render = render

app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")


@app.middleware("http")
async def session_setup(request: Request, call_next):
    request.state.user = None
    if "csrf" not in request.session:
        request.session["csrf"] = csrf_token()
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


@app.get("/health")
def health(db: Session = Depends(get_db)):
    db.execute(text("SELECT 1"))
    return JSONResponse({"status": "ok", "app": settings.app_name})


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
