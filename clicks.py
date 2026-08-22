"""
clicks.py — job-listing click tracking from the Resume Builder site.

Logs which job listing a visitor interacted with (opened the posting, or
clicked "Generate Resume" for it) and how. Same posture as resumes.py: the
frontend sends this deliberately, and admin read access fails closed
without a token.

Endpoints:
  POST /api/job-clicks  — public: log one click
  GET  /api/job-clicks  — admin: list recent clicks (requires ?token=)

Env:
  RESUME_ADMIN_TOKEN  — same shared secret as resumes.py/admin.py
  SQLITE_PATH         — same db file (default ./searches.db)
"""
from __future__ import annotations

import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

router = APIRouter(prefix="/api/job-clicks", tags=["job-clicks"])

DB_PATH = Path(os.getenv("SQLITE_PATH", "./searches.db"))
DB_PATH.parent.mkdir(parents=True, exist_ok=True)
ADMIN_TOKEN = os.getenv("RESUME_ADMIN_TOKEN", "")

_db_lock = threading.Lock()

ACTIONS = ("open_title", "open_link", "generate_resume")


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
                CREATE TABLE IF NOT EXISTS job_clicks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    clicked_at TEXT NOT NULL,
                    action TEXT NOT NULL,
                    job_title TEXT,
                    company TEXT,
                    site TEXT,
                    url TEXT,
                    search_term TEXT,
                    session_id TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_job_clicks_clicked_at
                    ON job_clicks(clicked_at DESC);
                """
            )
            conn.commit()
        finally:
            conn.close()


_init_db()


def _require_admin(token: str | None) -> None:
    if not ADMIN_TOKEN:
        raise HTTPException(status_code=503, detail="Admin access not configured (RESUME_ADMIN_TOKEN unset).")
    if token != ADMIN_TOKEN:
        raise HTTPException(status_code=401, detail="Invalid or missing token.")


class JobClick(BaseModel):
    action: str
    job_title: str = ""
    company: str = ""
    site: str = ""
    url: str = ""
    search_term: str = ""
    session_id: str = ""


@router.post("")
async def log_click(payload: JobClick) -> dict:
    if payload.action not in ACTIONS:
        raise HTTPException(status_code=400, detail="Unknown action.")
    now = datetime.now(timezone.utc).isoformat()
    with _db_lock:
        conn = _connect()
        try:
            conn.execute(
                """
                INSERT INTO job_clicks
                    (clicked_at, action, job_title, company, site, url, search_term, session_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (now, payload.action, payload.job_title, payload.company,
                 payload.site, payload.url, payload.search_term, payload.session_id),
            )
            conn.commit()
        finally:
            conn.close()
    return {"ok": True}


@router.get("")
async def list_clicks(token: str | None = Query(None), limit: int = Query(200, ge=1, le=1000)) -> dict:
    _require_admin(token)
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT id, clicked_at, action, job_title, company, site, url, search_term, session_id "
            "FROM job_clicks ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return {"count": len(rows), "clicks": [dict(r) for r in rows]}
    finally:
        conn.close()
