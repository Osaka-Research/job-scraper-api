"""
resumes.py — talent-pool submissions from the Resume Builder site.

The Resume Builder (a separate static site) is local-only by default: typed
data lives in that browser's localStorage and never leaves the device unless
the visitor explicitly clicks "Submit to talent pool" on the final resume
step. That button POSTs here. There is no silent/automatic collection --
every row in this table is something a visitor deliberately chose to send.

Endpoints:
  POST /api/resumes         — public: a visitor submits their resume
  GET  /api/resumes         — admin: list all submissions (requires ?token=)
  GET  /api/resumes/export  — admin: xlsx of all submissions (requires ?token=)

Env:
  RESUME_ADMIN_TOKEN  — shared secret required to read/export submissions.
                        If unset, the admin endpoints refuse all requests
                        (fail closed, not open) rather than exposing PII
                        with no protection at all.
  SQLITE_PATH         — same db file admin.py uses (default ./searches.db)
"""
from __future__ import annotations

import io
import json
import logging
import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

import openpyxl
from fastapi import APIRouter, HTTPException, Query, Response
from pydantic import BaseModel, Field

log = logging.getLogger("agent-jobs.resumes")

router = APIRouter(prefix="/api/resumes", tags=["resumes"])

DB_PATH = Path(os.getenv("SQLITE_PATH", "./searches.db"))
DB_PATH.parent.mkdir(parents=True, exist_ok=True)
ADMIN_TOKEN = os.getenv("RESUME_ADMIN_TOKEN", "")

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
                CREATE TABLE IF NOT EXISTS resumes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    submitted_at TEXT NOT NULL,
                    name TEXT,
                    headline TEXT,
                    email TEXT,
                    phone TEXT,
                    location TEXT,
                    link TEXT,
                    summary TEXT,
                    skills TEXT,
                    raw_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_resumes_submitted_at
                    ON resumes(submitted_at DESC);
                """
            )
            conn.commit()
        finally:
            conn.close()


_init_db()


class ExperienceEntry(BaseModel):
    title: str = ""
    company: str = ""
    location: str = ""
    start: str = ""
    end: str = ""
    bullets: list[str] = Field(default_factory=list)


class EducationEntry(BaseModel):
    degree: str = ""
    school: str = ""
    location: str = ""
    start: str = ""
    end: str = ""


class ResumeSubmission(BaseModel):
    name: str = ""
    headline: str = ""
    email: str = ""
    phone: str = ""
    location: str = ""
    link: str = ""
    summary: str = ""
    skills: str = ""
    experience: list[ExperienceEntry] = Field(default_factory=list)
    education: list[EducationEntry] = Field(default_factory=list)


def _require_admin(token: str | None) -> None:
    if not ADMIN_TOKEN:
        raise HTTPException(status_code=503, detail="Admin access not configured (RESUME_ADMIN_TOKEN unset).")
    if token != ADMIN_TOKEN:
        raise HTTPException(status_code=401, detail="Invalid or missing token.")


@router.post("")
async def submit_resume(payload: ResumeSubmission) -> dict:
    if not (payload.name or payload.email or payload.summary):
        raise HTTPException(status_code=400, detail="Empty submission.")

    now = datetime.now(timezone.utc).isoformat()
    with _db_lock:
        conn = _connect()
        try:
            conn.execute(
                """
                INSERT INTO resumes
                    (submitted_at, name, headline, email, phone, location, link, summary, skills, raw_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    now, payload.name, payload.headline, payload.email, payload.phone,
                    payload.location, payload.link, payload.summary, payload.skills,
                    payload.model_dump_json(),
                ),
            )
            conn.commit()
        finally:
            conn.close()

    log.info(f"resume submitted: name={payload.name!r} email={payload.email!r}")
    return {"ok": True}


@router.get("")
async def list_resumes(token: str | None = Query(None)) -> dict:
    _require_admin(token)
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT id, submitted_at, name, headline, email, phone, location, link, summary, skills, raw_json "
            "FROM resumes ORDER BY id DESC"
        ).fetchall()
        return {
            "count": len(rows),
            "resumes": [
                {**{k: r[k] for k in r.keys() if k != "raw_json"}, "full": json.loads(r["raw_json"])}
                for r in rows
            ],
        }
    finally:
        conn.close()


@router.get("/export")
async def export_resumes(token: str | None = Query(None)) -> Response:
    _require_admin(token)
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT id, submitted_at, name, headline, email, phone, location, link, summary, skills "
            "FROM resumes ORDER BY id DESC"
        ).fetchall()
    finally:
        conn.close()

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Resumes"
    headers = ["ID", "Submitted", "Name", "Headline", "Email", "Phone", "Location", "Link", "Summary", "Skills"]
    ws.append(headers)
    for cell in ws[1]:
        cell.font = openpyxl.styles.Font(bold=True)
    for r in rows:
        ws.append([r["id"], r["submitted_at"], r["name"], r["headline"], r["email"],
                   r["phone"], r["location"], r["link"], r["summary"], r["skills"]])
    for col in ws.columns:
        width = max((len(str(c.value or "")) for c in col), default=10)
        ws.column_dimensions[col[0].column_letter].width = min(max(width + 2, 10), 60)

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    fname = f"resumes-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}.xlsx"
    return Response(
        content=buf.read(),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )
