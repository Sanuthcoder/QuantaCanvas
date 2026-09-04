"""GitHub Actions worker: drains the QuantaCanvas generation queue.

Run modes
---------
python worker.py --drain          # process every queued job, then exit (CI default)
python worker.py --once           # process at most one job
python worker.py --loop 300       # keep polling for 300 seconds (local dev)

Environment
-----------
SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY, GEMINI_API_KEYS  (required)
GEMINI_VISUALIZATION_MODELS                               (optional, comma list)
"""

import argparse
import re
import sys
import time
import traceback

from backend.gemini import ContentBlocked, ModelExhausted, generate_text
from backend.prompt import PROMPT
from backend.queue_store import claim_job, finish_job, queue_depth

KATEX_RECOVERY = """
<script>
(function () {
    const options = {
        delimiters: [
            { left: '$$', right: '$$', display: true },
            { left: '$', right: '$', display: false },
            { left: '\\\\(', right: '\\\\)', display: false },
            { left: '\\\\[', right: '\\\\]', display: true }
        ],
        throwOnError: false,
        strict: 'ignore',
        ignoredTags: ['script', 'noscript', 'style', 'textarea', 'pre', 'code', 'option']
    };
    function recoverMath() {
        if (typeof window.renderMathInElement === 'function' && document.body) {
            try { window.renderMathInElement(document.body, options); } catch (error) {
                console.warn('QuantaCanvas KaTeX recovery failed:', error);
            }
        }
    }
    function scheduleRecovery() {
        window.setTimeout(recoverMath, 0);
        window.setTimeout(recoverMath, 250);
        window.setTimeout(recoverMath, 1000);
    }
    document.addEventListener('DOMContentLoaded', scheduleRecovery, { once: true });
    window.addEventListener('load', scheduleRecovery, { once: true });
})();
</script>
"""


def clean_document(content: str) -> str:
    match = re.search(r"<!DOCTYPE html>.*</html>", content, re.DOTALL | re.IGNORECASE)
    if not match:
        raise ValueError("Model output did not contain a complete HTML document.")
    document = match.group(0).strip().replace("```html", "").replace("```", "")
    if "QuantaCanvas KaTeX recovery" not in document and "</head>" in document.lower():
        head_end = re.search(r"</head\s*>", document, re.IGNORECASE)
        document = document[: head_end.start()] + KATEX_RECOVERY + document[head_end.start():]
    return document


def extract_summary(content: str) -> str:
    match = re.search(
        r"<<<LESSON_SUMMARY>>>(.*?)<<<END_LESSON_SUMMARY>>>", content, re.DOTALL | re.IGNORECASE
    )
    return match.group(1).strip() if match else ""


def process_job(job: dict) -> None:
    job_id = job["id"]
    started = time.perf_counter()
    print(f"[worker] job {job_id} (attempt {job['attempts']}) starting", flush=True)

    try:
        content, model = generate_text(f"{PROMPT}\n\nUSER REQUEST: {job['prompt']}")
        html = clean_document(content)
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        finish_job(
            job_id,
            "completed",
            html=html,
            summary=extract_summary(content),
            model=model,
            generation_time_ms=elapsed_ms,
        )
        print(f"[worker] job {job_id} completed in {elapsed_ms} ms on {model}", flush=True)

    except ContentBlocked as error:
        # Never retry a blocked prompt - the answer will not change.
        finish_job(job_id, "failed", error=str(error), generation_time_ms=0)
        print(f"[worker] job {job_id} blocked by safety filters", flush=True)

    except (ModelExhausted, ValueError, Exception) as error:  # noqa: BLE001
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        message = str(error) or type(error).__name__
        outcome = finish_job(job_id, "failed", error=message, generation_time_ms=elapsed_ms)
        print(f"[worker] job {job_id} failed ({message}); next status: {outcome}", flush=True)
        traceback.print_exc()


def drain(max_jobs: int = 25) -> int:
    processed = 0
    while processed < max_jobs:
        job = claim_job()
        if not job:
            break
        process_job(job)
        processed += 1
    return processed


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--drain", action="store_true", help="process all queued jobs and exit")
    parser.add_argument("--once", action="store_true", help="process a single job and exit")
    parser.add_argument("--loop", type=int, default=0, help="poll for N seconds")
    parser.add_argument("--max-jobs", type=int, default=25)
    args = parser.parse_args()

    print(f"[worker] started; queue depth={queue_depth()}", flush=True)

    if args.once:
        job = claim_job()
        if job:
            process_job(job)
        else:
            print("[worker] nothing queued", flush=True)
        return 0

    if args.loop:
        deadline = time.time() + args.loop
        while time.time() < deadline:
            if drain(args.max_jobs) == 0:
                time.sleep(5)
        return 0

    processed = drain(args.max_jobs)
    print(f"[worker] drained {processed} job(s)", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
