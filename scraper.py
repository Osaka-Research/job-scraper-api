"""
scraper.py — thin async wrapper around python-jobspy.

The jobspy library is synchronous and serializes per-call; we run it in a
thread pool with a hard timeout, and gate concurrent calls with an asyncio
semaphore so we don't fork-bomb the upstream job boards.

Returned dicts are normalized to a stable shape so the frontend doesn't need
to know about jobspy's per-site quirks.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
from typing import Any

import pandas as pd
from jobspy import scrape_jobs

log = logging.getLogger("agent-jobs.scraper")

DEFAULT_SITES = [s.strip() for s in os.getenv("SCRAPE_DEFAULT_SITES", "linkedin,indeed,glassdoor").split(",") if s.strip()]
DEFAULT_HOURS_OLD = int(os.getenv("SCRAPE_DEFAULT_HOURS_OLD", "168"))
DEFAULT_RESULTS_WANTED = int(os.getenv("SCRAPE_MAX_PER_SITE", "50"))
DEFAULT_TIMEOUT_SECONDS = int(os.getenv("SCRAPE_TIMEOUT_SECONDS", "90"))

ALLOWED_SITES = {"linkedin", "indeed", "glassdoor", "zip_recruiter", "google"}

# global semaphore — limits concurrent scrapes regardless of which endpoint hit
_SEM: asyncio.Semaphore | None = None


def get_semaphore() -> asyncio.Semaphore:
    global _SEM
    if _SEM is None:
        _SEM = asyncio.Semaphore(2)
    return _SEM


def _job_to_dict(row: Any) -> dict[str, Any]:
    """jobspy returns a pandas DataFrame; normalize each row to a clean dict."""
    def _g(key: str, default: Any = None) -> Any:
        try:
            v = row.get(key)
            if v is None or (isinstance(v, float) and pd.isna(v)):
                return default
            return v
        except Exception:
            return default

    def _clean_text(s: Any) -> str:
        if not s:
            return ""
        # strip excessive whitespace and html
        s = re.sub(r"<[^>]+>", " ", str(s))
        s = re.sub(r"\s+", " ", s).strip()
        return s

    return {
        "id": str(_g("id") or _g("job_url") or ""),
        "title": _clean_text(_g("title")),
        "company": _clean_text(_g("company")),
        "location": _clean_text(_g("location")),
        "site": _g("site", ""),
        "url": _g("job_url", ""),
        "date_posted": str(_g("date_posted")) if _g("date_posted") is not None else None,
        "salary_min": _g("min_amount"),
        "salary_max": _g("max_amount"),
        "salary_currency": _g("currency"),
        "interval": _g("interval"),
        "description": _clean_text(_g("description"))[:2000],  # cap size
        "is_remote": bool(_g("is_remote")),
        "job_type": _g("job_type"),
    }


def _scrape_sync(
    sites: list[str],
    search_term: str,
    location: str,
    hours_old: int,
    results_wanted: int,
    country_indeed: str,
) -> list[dict[str, Any]]:
    """the actual blocking jobspy call. runs in a thread."""
    df: pd.DataFrame = scrape_jobs(
        site_name=sites,
        search_term=search_term,
        location=location or "USA",
        hours_old=hours_old,
        results_wanted=results_wanted,
        country_indeed=country_indeed,
        description_format="markdown",
        verbose=0,
    )
    if df is None or df.empty:
        return []
    # deduplicate by job_url — different sites sometimes return the same posting
    df = df.drop_duplicates(subset=["job_url"], keep="first")
    return [_job_to_dict(row) for _, row in df.iterrows()]


def _scrape_one_site_sync(
    site: str,
    search_term: str,
    location: str,
    hours_old: int,
    results_wanted: int,
    country_indeed: str,
) -> list[dict[str, Any]]:
    """Same as _scrape_sync but for exactly one site -- lets the caller dispatch
    sites as separate thread-pool tasks so each site's results are available as
    soon as that site finishes, instead of waiting on the slowest one."""
    return _scrape_sync([site], search_term, location, hours_old, results_wanted, country_indeed)


async def scrape_streaming(
    search_term: str,
    location: str = "",
    sites: list[str] | None = None,
    hours_old: int | None = None,
    results_wanted: int | None = None,
    timeout_seconds: int | None = None,
    country_indeed: str = "usa",
):
    """Same inputs/validation as scrape(), but yields one dict per site as soon as
    that site's scrape finishes, instead of one combined result at the end. Each
    yielded dict: {site, ok, jobs, count, error?}. A final {done: true, ...} dict
    closes the stream with the merged totals."""
    if not search_term or not search_term.strip():
        raise ValueError("search_term is required")

    sites = sites or DEFAULT_SITES
    sites = [s for s in sites if s in ALLOWED_SITES]
    if not sites:
        raise ValueError(f"no valid sites; allowed: {sorted(ALLOWED_SITES)}")

    hours_old = hours_old if hours_old is not None else DEFAULT_HOURS_OLD
    results_wanted = results_wanted if results_wanted is not None else DEFAULT_RESULTS_WANTED
    timeout_seconds = timeout_seconds or DEFAULT_TIMEOUT_SECONDS
    results_wanted = max(1, min(int(results_wanted), 200))
    hours_old = max(1, min(int(hours_old), 24 * 30))

    sem = get_semaphore()
    async with sem:
        log.info(f"streaming scrape start: term={search_term!r} loc={location!r} sites={sites} limit={results_wanted}")
        loop = asyncio.get_event_loop()
        tasks = {
            loop.run_in_executor(
                None, _scrape_one_site_sync, site, search_term, location, hours_old, results_wanted, country_indeed
            ): site
            for site in sites
        }

        seen_urls: set[str] = set()
        total = 0
        deadline = loop.time() + timeout_seconds

        pending = set(tasks.keys())
        while pending:
            remaining = deadline - loop.time()
            if remaining <= 0:
                for t in pending:
                    t.cancel()
                for t in pending:
                    yield {"site": tasks[t], "ok": False, "error": "timeout", "jobs": [], "count": 0}
                break

            done, pending = await asyncio.wait(pending, timeout=remaining, return_when=asyncio.FIRST_COMPLETED)
            for t in done:
                site = tasks[t]
                try:
                    jobs = t.result()
                    # cross-site duplicates (same posting mirrored on two boards)
                    fresh = [j for j in jobs if j.get("url") not in seen_urls]
                    for j in fresh:
                        seen_urls.add(j.get("url"))
                    total += len(fresh)
                    log.info(f"site done: {site} -> {len(fresh)} new job(s)")
                    yield {"site": site, "ok": True, "jobs": fresh, "count": len(fresh)}
                except Exception as e:
                    log.exception(f"site failed: {site}")
                    yield {"site": site, "ok": False, "error": str(e)[:300], "jobs": [], "count": 0}

        yield {"done": True, "count": total, "sites": sites}


async def scrape(
    search_term: str,
    location: str = "",
    sites: list[str] | None = None,
    hours_old: int | None = None,
    results_wanted: int | None = None,
    timeout_seconds: int | None = None,
    country_indeed: str = "usa",
) -> dict[str, Any]:
    """public scrape entry. validates inputs, gates concurrency, enforces timeout."""
    if not search_term or not search_term.strip():
        raise ValueError("search_term is required")

    sites = sites or DEFAULT_SITES
    sites = [s for s in sites if s in ALLOWED_SITES]
    if not sites:
        raise ValueError(f"no valid sites; allowed: {sorted(ALLOWED_SITES)}")

    hours_old = hours_old if hours_old is not None else DEFAULT_HOURS_OLD
    results_wanted = results_wanted if results_wanted is not None else DEFAULT_RESULTS_WANTED
    timeout_seconds = timeout_seconds or DEFAULT_TIMEOUT_SECONDS

    # soft cap on results_wanted — protect upstream
    results_wanted = max(1, min(int(results_wanted), 200))
    hours_old = max(1, min(int(hours_old), 24 * 30))  # max 30 days

    sem = get_semaphore()
    async with sem:
        log.info(f"scrape start: term={search_term!r} loc={location!r} sites={sites} limit={results_wanted}")
        try:
            loop = asyncio.get_event_loop()
            jobs = await asyncio.wait_for(
                loop.run_in_executor(
                    None,
                    _scrape_sync,
                    sites,
                    search_term,
                    location,
                    hours_old,
                    results_wanted,
                    country_indeed,
                ),
                timeout=timeout_seconds,
            )
        except asyncio.TimeoutError:
            log.warning(f"scrape timed out after {timeout_seconds}s")
            return {
                "ok": False,
                "error": "timeout",
                "message": f"scrape exceeded {timeout_seconds}s",
                "jobs": [],
                "count": 0,
                "sites": sites,
                "search_term": search_term,
                "location": location,
            }
        except Exception as e:
            log.exception("scrape failed")
            return {
                "ok": False,
                "error": "scrape_failed",
                "message": str(e)[:500],
                "jobs": [],
                "count": 0,
                "sites": sites,
                "search_term": search_term,
                "location": location,
            }

    # per-site breakdown -- a merged list alone hides which sites actually returned
    # anything, and callers have no way to tell "site returned 0" from "site was never
    # asked". Every requested site gets a count here, including the zeros.
    site_counts = {s: 0 for s in sites}
    for j in jobs:
        if j.get("site") in site_counts:
            site_counts[j["site"]] += 1

    log.info(f"scrape done: {len(jobs)} unique jobs, by site: {site_counts}")
    return {
        "ok": True,
        "jobs": jobs,
        "count": len(jobs),
        "sites": sites,
        "site_counts": site_counts,
        "search_term": search_term,
        "location": location,
        "hours_old": hours_old,
        "results_wanted": results_wanted,
    }
