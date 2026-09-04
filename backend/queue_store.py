"""Supabase-backed job queue shared by the Vercel API and the GitHub Actions worker.

The Vercel function only ever enqueues and reads status, so it always returns in
well under a second and can never hit the 300s function limit. The worker does
the slow Gemini call in GitHub Actions, where a job may run for hours.
"""

import os
import time

from dotenv import load_dotenv
from supabase import Client, create_client

load_dotenv()

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").strip()
SUPABASE_SERVICE_ROLE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "").strip()

supabase: Client | None = (
    create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)
    if SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY
    else None
)

# A worker run that dies mid-job leaves the row in `running`; after this long any
# worker may pick it up again.
STALE_JOB_SECONDS = int(os.environ.get("QUEUE_STALE_SECONDS", "900"))
MAX_ATTEMPTS = int(os.environ.get("QUEUE_MAX_ATTEMPTS", "3"))


def _client() -> Client:
    if not supabase:
        raise RuntimeError("Supabase is not configured (SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY).")
    return supabase


def enqueue_job(generation_id: str, user_id: str, prompt: str) -> str:
    """Create the queue row for an already-reserved generation."""
    _client().table("generation_jobs").upsert(
        {
            "id": generation_id,
            "user_id": user_id,
            "prompt": prompt,
            "status": "queued",
            "attempts": 0,
        },
        on_conflict="id",
    ).execute()
    return generation_id


def claim_job() -> dict | None:
    """Take one job off the queue, or return None when the queue is empty."""
    result = _client().rpc(
        "claim_generation_job", {"p_stale_seconds": STALE_JOB_SECONDS}
    ).execute()
    rows = result.data or []
    return rows[0] if rows else None


def finish_job(
    job_id: str,
    status: str,
    html: str | None = None,
    summary: str | None = None,
    error: str | None = None,
    model: str | None = None,
    generation_time_ms: int | None = None,
) -> str:
    """Store the result. A failure with attempts left is re-queued automatically."""
    result = _client().rpc(
        "finish_generation_job",
        {
            "p_job_id": job_id,
            "p_status": status,
            "p_html": html,
            "p_summary": summary,
            "p_error": error,
            "p_model": model,
            "p_generation_time_ms": generation_time_ms,
            "p_max_attempts": MAX_ATTEMPTS,
        },
    ).execute()
    return result.data if isinstance(result.data, str) else status


def job_status(job_id: str, include_html: bool = True) -> dict | None:
    """Read a job for the browser poll on the responses page."""
    columns = "id,status,prompt,attempts,error_message,created_at,generation_time_ms,result_summary"
    if include_html:
        columns += ",result_html"
    result = (
        _client()
        .table("generation_jobs")
        .select(columns)
        .eq("id", job_id)
        .limit(1)
        .execute()
    )
    rows = result.data or []
    return rows[0] if rows else None


def queue_depth() -> int:
    result = (
        _client()
        .table("generation_jobs")
        .select("id", count="exact")
        .in_("status", ["queued", "running"])
        .execute()
    )
    return result.count or 0


def wait_for_status(job_id: str, timeout: float = 30.0, interval: float = 2.0) -> dict | None:
    """Only used by local scripts/tests; the website polls over HTTP instead."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        row = job_status(job_id)
        if row and row["status"] in ("completed", "failed"):
            return row
        time.sleep(interval)
    return job_status(job_id)
