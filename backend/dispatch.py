"""Kick off the GitHub Actions worker the instant a prompt arrives.

No manual clicking: `repository_dispatch` starts a workflow run over the API.
The workflow also has a schedule as a safety net, so a job never sits forever if
the dispatch call fails (GitHub outage, expired token).

Setup
-----
1. Create a fine-grained personal access token with, for this repo only,
   "Contents: read and write" (that permission is what unlocks dispatch).
2. Add it to Vercel as GITHUB_DISPATCH_TOKEN, plus:
   GITHUB_REPOSITORY = "your-username/your-repo"
"""

import json
import os
import urllib.error
import urllib.request

GITHUB_API = "https://api.github.com"
DISPATCH_TOKEN = os.environ.get("GITHUB_DISPATCH_TOKEN", "").strip()
REPOSITORY = os.environ.get("GITHUB_REPOSITORY", "").strip()
EVENT_TYPE = os.environ.get("GITHUB_DISPATCH_EVENT", "quantacanvas-generate")


def trigger_worker(job_id: str) -> bool:
    """Best effort. Returns False when not configured or GitHub rejected it."""
    if not DISPATCH_TOKEN or not REPOSITORY:
        print("[dispatch] skipped: GITHUB_DISPATCH_TOKEN / GITHUB_REPOSITORY not set", flush=True)
        return False

    request = urllib.request.Request(
        f"{GITHUB_API}/repos/{REPOSITORY}/dispatches",
        method="POST",
        data=json.dumps({"event_type": EVENT_TYPE, "client_payload": {"job_id": job_id}}).encode(),
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {DISPATCH_TOKEN}",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
            "User-Agent": "quantacanvas",
        },
    )

    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            print(f"[dispatch] worker triggered for {job_id} (HTTP {response.status})", flush=True)
            return 200 <= response.status < 300
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", "replace")[:300]
        print(f"[dispatch] GitHub rejected the trigger [{error.code}]: {body}", flush=True)
        return False
    except Exception as error:  # noqa: BLE001 - never block the user's request
        print(f"[dispatch] trigger failed: {type(error).__name__}: {error}", flush=True)
        return False
