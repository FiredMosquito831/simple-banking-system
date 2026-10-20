"""Runtime settings, database explorer, SQL console, backups and audit log."""
from __future__ import annotations

import csv
import io
import os
from datetime import datetime

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import RedirectResponse, StreamingResponse
from sqlalchemy import MetaData, Table, func, inspect, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.schema import CreateTable
from sqlalchemy.orm import Session

from app.db import engine, get_db
from app.models import AuditLog
from app.services import settings as rules
from app.services.banking import audit
from app.utils import flash

from .deps import admin_user, client_ip, render, verify_csrf

router = APIRouter()
PAGE_SIZE = 25
READONLY_PREFIXES = ("select", "show", "describe", "desc", "explain")
BLOCKED_PREFIXES = ("drop", "truncate", "alter", "create", "rename", "grant", "revoke", "lock", "kill")


def _page(request: Request) -> int:
    try:
        return max(1, int(request.query_params.get("page", 1)))
    except ValueError:
        return 1


# --- Settings --------------------------------------------------------------
@router.get("/settings")
def settings_page(request: Request, admin=Depends(admin_user), db: Session = Depends(get_db)):
    groups = rules.grouped_rows(db)
    return render(request, "settings.html", title="Institution settings", groups=groups)


@router.post("/settings")
async def settings_save(
    request: Request,
    admin=Depends(admin_user),
    db: Session = Depends(get_db),
):
    """Save every `setting.<key>` field posted by the settings page."""
    form = await request.form()
    if form.get("csrf") != request.session.get("csrf"):
        from fastapi import HTTPException

        raise HTTPException(status_code=400, detail="Your session expired. Refresh and try again.")

    changed: list[str] = []
    for field in form.keys():
        if not field.startswith("setting."):
            continue
        key = field[len("setting."):]
        values = form.getlist(field)
        new_value = str(values[-1]) if values else ""
        old_value = rules.get_value(db, key)
        if new_value == old_value:
            continue
        if key in rules._DEFAULTS and rules._DEFAULTS[key]["value_type"] == "int":
            try:
                int(float(new_value))
            except ValueError:
                flash(request, "error", f"{key} must be a whole number.")
                return RedirectResponse("/settings", status_code=303)
        rules.set_value(db, key, new_value)
        changed.append(f"{key}: {old_value} → {new_value}")

    if changed:
        audit(db, admin, "admin.settings_update", "settings", "", "; ".join(changed)[:2000], client_ip(request))
        db.commit()
        rules.warm_cache(db)
    flash(request, "success", f"Saved {len(changed)} setting(s)." if changed else "No changes to save.")
    return RedirectResponse("/settings", status_code=303)


@router.get("/audit")
def audit_log(
    request: Request,
    q: str = Query(""),
    admin=Depends(admin_user),
    db: Session = Depends(get_db),
):
    stmt = select(AuditLog).order_by(AuditLog.created_at.desc())
    if q:
        like = f"%{q.strip()}%"
        stmt = stmt.where(
            AuditLog.action.ilike(like) | AuditLog.actor_label.ilike(like) | AuditLog.details.ilike(like)
        )
    page = _page(request)
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    logs = db.scalars(stmt.limit(PAGE_SIZE).offset((page - 1) * PAGE_SIZE)).all()
    return render(
        request, "audit.html", title="Audit trail", logs=logs, q=q,
        page=page, pages=max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE), total=total,
    )


# --- Database explorer -----------------------------------------------------
def _tables() -> list[str]:
    return sorted(inspect(engine).get_table_names())


@router.get("/database")
def database(request: Request, admin=Depends(admin_user), db: Session = Depends(get_db)):
    inspector = inspect(engine)
    rows = []
    for name in _tables():
        try:
            count = db.execute(text(f"SELECT COUNT(*) FROM `{name}`")).scalar() or 0
        except SQLAlchemyError:
            count = -1
        try:
            columns = len(inspector.get_columns(name))
        except SQLAlchemyError:
            columns = 0
        rows.append({"name": name, "rows": count, "columns": columns})
    with engine.connect() as conn:
        version = conn.execute(text("SELECT VERSION()")).scalar()
        database_name = conn.execute(text("SELECT DATABASE()")).scalar()
    return render(
        request, "database.html", title="Database explorer", tables=rows,
        version=version, database_name=database_name, tables_count=len(rows),
    )


def _load_table(name: str) -> Table:
    if name not in _tables():
        raise ValueError("Unknown table")
    return Table(name, MetaData(), autoload_with=engine)


@router.get("/database/backup.sql")
def database_backup(request: Request, admin=Depends(admin_user), db: Session = Depends(get_db)):
    metadata = MetaData()
    metadata.reflect(bind=engine)
    out = io.StringIO()
    out.write(f"-- Danube Ops backup generated {datetime.utcnow():%Y-%m-%d %H:%M} UTC\n")
    out.write("SET FOREIGN_KEY_CHECKS=0;\n\n")
    for table in metadata.sorted_tables:
        out.write(f"-- Table {table.name}\n")
        out.write(str(CreateTable(table).compile(engine)).strip() + ";\n")
        rows = db.execute(select(table)).mappings().all()
        if rows:
            columns = ", ".join(f"`{c.name}`" for c in table.columns)
            for row in rows:
                values = ", ".join(_sql_literal(row.get(c.name)) for c in table.columns)
                out.write(f"INSERT INTO `{table.name}` ({columns}) VALUES ({values});\n")
        out.write("\n")
    out.write("SET FOREIGN_KEY_CHECKS=1;\n")
    audit(db, admin, "admin.backup", "database", "all", "SQL dump downloaded", client_ip(request))
    db.commit()
    filename = f"danube-ops-{datetime.utcnow():%Y%m%d-%H%M}.sql"
    return StreamingResponse(
        iter([out.getvalue()]),
        media_type="application/sql",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/database/{name}")
def table_view(
    name: str,
    request: Request,
    sort: str = Query(""),
    dir: str = Query("asc"),
    admin=Depends(admin_user),
    db: Session = Depends(get_db),
):
    try:
        table = _load_table(name)
    except ValueError:
        flash(request, "error", "Table not found.")
        return RedirectResponse("/database", status_code=303)
    page = _page(request)
    total = db.execute(select(func.count()).select_from(table)).scalar() or 0
    order_col = table.c.get(sort) if sort in table.c else next(iter(table.primary_key.columns), None)
    stmt = select(table)
    if order_col is not None:
        stmt = stmt.order_by(order_col.desc() if dir == "desc" else order_col.asc())
    rows = db.execute(stmt.limit(PAGE_SIZE).offset((page - 1) * PAGE_SIZE)).mappings().all()
    return render(
        request, "table.html", title=f"Table · {name}", table=table, rows=rows, table_name=name, sort=sort,
        direction=dir, page=page, pages=max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE), total=total,
    )


@router.get("/database/{name}/export.csv")
def table_export(name: str, request: Request, admin=Depends(admin_user), db: Session = Depends(get_db)):
    try:
        table = _load_table(name)
    except ValueError:
        flash(request, "error", "Table not found.")
        return RedirectResponse("/database", status_code=303)
    rows = db.execute(select(table)).mappings().all()
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    columns = [c.name for c in table.columns]
    writer.writerow(columns)
    for row in rows:
        writer.writerow([row.get(c) for c in columns])
    buffer.seek(0)
    audit(db, admin, "admin.table_export", "table", name, f"{len(rows)} rows", client_ip(request))
    db.commit()
    return StreamingResponse(
        iter([buffer.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{name}-{datetime.utcnow():%Y%m%d}.csv"'},
    )


def _sql_literal(value) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, (datetime,)):
        return "'" + value.strftime("%Y-%m-%d %H:%M:%S") + "'"
    escaped = str(value).replace("\\", "\\\\").replace("'", "''")
    return f"'{escaped}'"


# --- SQL console -----------------------------------------------------------
@router.get("/sql")
def sql_page(request: Request, admin=Depends(admin_user)):
    return render(
        request, "sql.html", title="SQL console",
        write_enabled=os.getenv("ALLOW_ADMIN_SQL_WRITE", "0") == "1",
    )


@router.post("/sql")
def sql_run(
    request: Request,
    statement: str = Form(""),
    _csrf: None = Depends(verify_csrf),
    admin=Depends(admin_user),
    db: Session = Depends(get_db),
):
    sql = statement.strip().rstrip(";")
    write_enabled = os.getenv("ALLOW_ADMIN_SQL_WRITE", "0") == "1"
    first = sql.split(None, 1)[0].lower() if sql else ""
    context = {"statement": sql, "rows": [], "columns": [], "rowcount": None, "error": None,
               "write_enabled": write_enabled, "title": "SQL console"}
    if not sql:
        context["error"] = "Enter a statement."
        return render(request, "sql.html", **context)
    if first in BLOCKED_PREFIXES:
        context["error"] = f"{first.upper()} statements are blocked in the console. Use migrations for schema changes."
        return render(request, "sql.html", **context)
    if first not in READONLY_PREFIXES and not write_enabled:
        context["error"] = "Write access is disabled. Set ALLOW_ADMIN_SQL_WRITE=1 to enable INSERT/UPDATE/DELETE."
        return render(request, "sql.html", **context)
    try:
        result = db.execute(text(sql))
        if result.returns_rows:
            rows = result.mappings().all()[:500]
            context["rows"] = rows
            context["columns"] = list(result.keys())
        else:
            context["rowcount"] = result.rowcount
            db.commit()
        audit(db, admin, "admin.sql", "database", first, sql[:800], client_ip(request))
        db.commit()
        flash(request, "success", f"Statement executed{' — ' + str(len(context['rows'])) + ' rows' if context['rows'] else ''}.")
    except SQLAlchemyError as exc:
        db.rollback()
        context["error"] = str(exc.__dict__.get("orig", exc))
    return render(request, "sql.html", **context)
