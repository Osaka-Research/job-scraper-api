"""
admin.py — sqlite log + xlsx export + telegram bot delivery.

Every successful scrape is logged here and (if TELEGRAM_BOT_TOKEN +
TELEGRAM_CHAT_ID are set) the xlsx is sent to that chat. one chat, no
subscribers, no broadcast, no polling.

Endpoints:
  POST /api/admin/log-search   — internal: called by main.py after each scrape
  GET  /api/admin/export      — admin: pull full xlsx of all logged searches
  GET  /api/admin/stats        — admin: row counts and last-search metadata

Env:
  TELEGRAM_BOT_TOKEN  — bot token for sending xlsx
  TELEGRAM_CHAT_ID    — chat id to send to
  SQLITE_PATH         — db file path (default /data/searches.db)
"""
from __future__ import annotations

import io
import logging
import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

import httpx
import openpyxl
from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel, Field

log = logging.getLogger("agent-jobs.admin")

router = APIRouter(prefix="/api/admin", tags=["admin"])

DB_PATH = Path(os.getenv("SQLITE_PATH", "/data/searches.db"))
DB_PATH.parent.mkdir(parents=True, exist_ok=True)
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

_db_lock = threading.Lock()


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def _init_db() -> None:
    with _db_lock:
        conn = _connect()
        try:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS searches (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    search_term TEXT NOT NULL,
                    location TEXT NOT NULL,
                    sites TEXT NOT NULL,
                    hours_old INTEGER NOT NULL,
                    results_wanted INTEGER NOT NULL,
                    job_count INTEGER NOT NULL,
                    ok INTEGER NOT NULL,
                    duration_seconds REAL
                );
                CREATE INDEX IF NOT EXISTS idx_searches_created_at
                    ON searches(created_at DESC);

                CREATE TABLE IF NOT EXISTS jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    search_id INTEGER NOT NULL REFERENCES searches(id) ON DELETE CASCADE,
                    title TEXT,
                    company TEXT,
                    location TEXT,
                    site TEXT,
                    url TEXT,
                    date_posted TEXT,
                    salary_min INTEGER,
                    salary_max INTEGER,
                    salary_currency TEXT,
                    interval TEXT,
                    is_remote INTEGER,
                    job_type TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_jobs_search_id ON jobs(search_id);
                """
            )
            conn.commit()
        finally:
            conn.close()


_init_db()


class LogJob(BaseModel):
    title: str | None = None
    company: str | None = None
    location: str | None = None
    site: str | None = None
    url: str | None = None
    date_posted: str | None = None
    salary_min: float | None = None
    salary_max: float | None = None
    salary_currency: str | None = None
    interval: str | None = None
    is_remote: bool | None = None
    job_type: str | None = None


class LogSearch(BaseModel):
    search_term: str
    location: str = ""
    sites: list[str] = Field(default_factory=list)
    hours_old: int = 168
    results_wanted: int = 50
    job_count: int = 0
    ok: bool = True
    duration_seconds: float | None = None
    jobs: list[LogJob] = Field(default_factory=list)


def _build_searches_workbook() -> openpyxl.Workbook:
    conn = _connect()
    try:
        searches = conn.execute("SELECT * FROM searches ORDER BY id DESC").fetchall()
        jobs = conn.execute(
            """SELECT j.id, j.search_id, j.title, j.company, j.location, j.site,
                       j.url, j.date_posted, j.salary_min, j.salary_max,
                       j.salary_currency, j.interval, j.is_remote, j.job_type,
                       s.created_at AS search_created_at,
                       s.search_term, s.location AS search_location
                 FROM jobs j JOIN searches s ON j.search_id = s.id
                ORDER BY j.id DESC"""
        ).fetchall()
    finally:
        conn.close()

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "searches"
    headers = ["id", "created_at", "search_term", "location", "sites",
               "hours_old", "results_wanted", "job_count", "ok", "duration_seconds"]
    ws.append(headers)
    _style_header(ws)
    for r in searches:
        ws.append([r[h] for h in headers])

    ws2 = wb.create_sheet("jobs")
    headers2 = ["id", "search_id", "search_created_at", "search_term", "search_location",
                "title", "company", "location", "site", "url", "date_posted",
                "salary_min", "salary_max", "salary_currency", "interval", "is_remote", "job_type"]
    ws2.append(headers2)
    _style_header(ws2)
    for j in jobs:
        ws2.append([j[h] for h in headers2])

    for sheet in (ws, ws2):
        for col_idx, _ in enumerate(sheet[1], start=1):
            col_letter = openpyxl.utils.cell.get_column_letter(col_idx)
            sheet.column_dimensions[col_letter].width = 28
    return wb


def _build_single_search_xlsx(payload: LogSearch) -> bytes:
    """build a tiny xlsx with just the search metadata + its jobs."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "search"
    ws.append(["search_term", payload.search_term])
    ws.append(["location", payload.location])
    ws.append(["sites", ",".join(payload.sites)])
    ws.append(["hours_old", payload.hours_old])
    ws.append(["results_wanted", payload.results_wanted])
    ws.append(["job_count", payload.job_count])
    ws.append(["ok", payload.ok])
    if payload.duration_seconds is not None:
        ws.append(["duration_seconds", payload.duration_seconds])
    ws.append([])
    hdr = ["title", "company", "location", "site", "url", "salary", "remote", "type"]
    ws.append(hdr)
    for cell in ws[ws.max_row]:
        cell.font = openpyxl.styles.Font(bold=True)
    for j in payload.jobs:
        sal = ""
        if j.salary_min and j.salary_max:
            sal = f"{j.salary_currency or ''} {j.salary_min}-{j.salary_max} {j.interval or ''}".strip()
        elif j.salary_min:
            sal = f"{j.salary_currency or ''} {j.salary_min} {j.interval or ''}".strip()
        ws.append([j.title, j.company, j.location, j.site, j.url, sal,
                   "yes" if j.is_remote else "", j.job_type])

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _style_header(ws) -> None:
    fill = openpyxl.styles.PatternFill(start_color="3ddc97", end_color="3ddc97", fill_type="solid")
    font = openpyxl.styles.Font(bold=True, color="0b0f0d")
    for cell in ws[1]:
        cell.fill = fill
        cell.font = font


async def _send_xlsx_to_bot(xlsx_bytes: bytes, filename: str, caption: str) -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log.info("telegram delivery skipped (TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set)")
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendDocument"
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            files = {"document": (filename, xlsx_bytes,
                                  "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")}
            data = {"chat_id": TELEGRAM_CHAT_ID, "caption": caption[:1024]}
            r = await client.post(url, files=files, data=data)
            if r.status_code != 200:
                log.warning(f"telegram sendDocument failed: {r.status_code} {r.text[:200]}")
                return False
            return True
    except Exception as e:
        log.exception(f"telegram sendDocument exception: {e}")
        return False


@router.post("/log-search")
async def log_search(payload: LogSearch) -> dict:
    """internal endpoint: log search + jobs + send xlsx to bot."""
    created_at = datetime.now(timezone.utc).isoformat()
    sites_csv = ",".join(payload.sites)

    with _db_lock:
        conn = _connect()
        try:
            cur = conn.execute(
                """INSERT INTO searches
                       (created_at, search_term, location, sites, hours_old,
                        results_wanted, job_count, ok, duration_seconds)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (created_at, payload.search_term, payload.location, sites_csv,
                 payload.hours_old, payload.results_wanted, payload.job_count,
                 1 if payload.ok else 0, payload.duration_seconds),
            )
            search_id = cur.lastrowid
            for j in payload.jobs:
                conn.execute(
                    """INSERT INTO jobs
                           (search_id, title, company, location, site, url,
                            date_posted, salary_min, salary_max, salary_currency,
                            interval, is_remote, job_type)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (search_id, j.title, j.company, j.location, j.site, j.url,
                     j.date_posted, j.salary_min, j.salary_max, j.salary_currency,
                     j.interval, 1 if j.is_remote else 0, j.job_type),
                )
            conn.commit()
        finally:
            conn.close()

    delivered = False
    if payload.ok and payload.jobs:
        xlsx = _build_single_search_xlsx(payload)
        fname = f"search-{search_id}-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}.xlsx"
        caption = (
            f"🔍 {payload.search_term!r}"
            + (f" @ {payload.location}" if payload.location else "")
            + f"\n📊 {payload.job_count} jobs"
        )
        delivered = await _send_xlsx_to_bot(xlsx, fname, caption)

    return {"ok": True, "search_id": search_id, "logged_jobs": len(payload.jobs),
            "telegram_delivered": delivered}


@router.get("/stats")
async def stats() -> dict:
    conn = _connect()
    try:
        n_searches = conn.execute("SELECT COUNT(*) AS n FROM searches").fetchone()["n"]
        n_jobs = conn.execute("SELECT COUNT(*) AS n FROM jobs").fetchone()["n"]
        last = conn.execute(
            "SELECT created_at, search_term, location, job_count FROM searches "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return {
            "searches_count": n_searches,
            "jobs_count": n_jobs,
            "last_search": dict(last) if last else None,
            "telegram_enabled": bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID),
        }
    finally:
        conn.close()


@router.get("/export")
async def export() -> Response:
    wb = _build_searches_workbook()
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    fname = f"agent-jobs-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}.xlsx"
    return Response(
        content=buf.read(),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )
