"""
main.py — fastapi app.

Routes:
  GET  /                  → dashboard (static HTML)
  GET  /api/health        → {ok, version, sites_supported}
  POST /api/scrape        → body: {search_term, location, sites, hours_old, results_wanted}
                          → returns normalized job list (see scraper.py)
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import scraper
import admin

log_level = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=log_level,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("agent-jobs.main")

VERSION = "1.0.0"

app = FastAPI(
    title="Agent Jobs",
    description="Job board scraper (linkedin/indeed/glassdoor) with a tiny dashboard.",
    version=VERSION,
)

# Local-only tool called from other localhost pages (e.g. the resume builder on a
# different port) -- no cookies/auth involved, so a wide-open localhost CORS policy
# is fine here and avoids fighting the browser for every dev port combination.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class ScrapeRequest(BaseModel):
    search_term: str = Field(..., min_length=1, max_length=200, description="job title / keywords")
    location: str = Field("", max_length=200, description='e.g. "Remote", "San Francisco", or empty')
    sites: list[str] | None = Field(None, description='subset of ["linkedin","indeed","glassdoor","zip_recruiter","google"]; default = all configured')
    hours_old: int | None = Field(None, ge=1, le=24 * 30, description="filter: posted within N hours")
    results_wanted: int | None = Field(None, ge=1, le=200, description="per-site cap")
    timeout_seconds: int | None = Field(None, ge=5, le=300, description="override default 90s timeout")
    country_indeed: str = Field("usa", max_length=50, description='country name for Indeed/Glassdoor, e.g. "usa", "india", "uk" -- see jobspy.model.Country for the full list')


@app.get("/api/health")
async def health() -> dict:
    return {
        "ok": True,
        "version": VERSION,
        "sites_supported": sorted(scraper.ALLOWED_SITES),
        "sites_default": scraper.DEFAULT_SITES,
        "timeout_default_s": scraper.DEFAULT_TIMEOUT_SECONDS,
    }


@app.post("/api/scrape")
async def scrape(req: ScrapeRequest) -> dict:
    import time as _time
    t0 = _time.time()
    try:
        result = await scraper.scrape(
            search_term=req.search_term,
            location=req.location,
            sites=req.sites,
            hours_old=req.hours_old,
            results_wanted=req.results_wanted,
            timeout_seconds=req.timeout_seconds,
            country_indeed=req.country_indeed,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    # log + send to telegram (best-effort, does not block the response)
    try:
        elapsed = round(_time.time() - t0, 2)
        await admin.log_search(admin.LogSearch(
            search_term=req.search_term,
            location=req.location or "",
            sites=result.get("sites") or [],
            hours_old=result.get("hours_old") or 168,
            results_wanted=result.get("results_wanted") or 50,
            job_count=result.get("count", 0),
            ok=result.get("ok", False),
            duration_seconds=elapsed,
            jobs=[admin.LogJob(**j) for j in result.get("jobs", [])],
        ))
    except Exception:
        log.exception("admin log_search failed (non-fatal)")

    return result


@app.post("/api/scrape-stream")
async def scrape_stream(req: ScrapeRequest):
    """Same inputs as /api/scrape, but responds with newline-delimited JSON: one
    line per site as soon as that site finishes, then a final {"done": true, ...}
    line. Lets the frontend show results incrementally instead of waiting for the
    slowest site (LinkedIn/Glassdoor can take 30-60s+ while Indeed is often done
    in a few seconds)."""
    async def body():
        try:
            async for chunk in scraper.scrape_streaming(
                search_term=req.search_term,
                location=req.location,
                sites=req.sites,
                hours_old=req.hours_old,
                results_wanted=req.results_wanted,
                timeout_seconds=req.timeout_seconds,
                country_indeed=req.country_indeed,
            ):
                yield json.dumps(chunk) + "\n"
        except ValueError as e:
            yield json.dumps({"done": True, "error": str(e), "count": 0}) + "\n"

    return StreamingResponse(body(), media_type="application/x-ndjson")


# serve the dashboard from app/static/
STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
async def root() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")
