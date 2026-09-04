# QuantaCanvas — generation in GitHub Actions (no more 300s Vercel timeout)

## Flow

```text
browser ──POST /api/generate──► Vercel        (returns a job id in <1s)
                                 ├─ writes a "queued" row in Supabase
                                 └─ repository_dispatch → GitHub Actions
GitHub Actions worker ──► Gemini              (up to 60 min per run)
                          └─ writes the finished HTML back to Supabase
browser /responses?job=<id> ──GET /api/generation/<id>── every 3s ──► renders it
```

Vercel never waits for Gemini, so `maxDuration` no longer matters (dropped to 60s).
`/responses?job=<id>` is durable: reload it, close the tab, open it on your phone later.

No button pressing: the workflow triggers on `repository_dispatch` (instant),
`schedule: */5 * * * *` (safety net if a dispatch fails), and `workflow_dispatch`
(manual, for debugging). Keep the repo public for free unlimited Actions minutes.

## Files in this change

| File | Role |
| --- | --- |
| `worker.py` | Drains the queue in Actions (`--drain`, `--once`, `--loop N`) |
| `backend/gemini.py` | Key rotation **plus model fallback** (see 429 below) |
| `backend/queue_store.py` | Enqueue / claim / finish / status via service role key |
| `backend/dispatch.py` | Fires `repository_dispatch` from Vercel |
| `backend/app.py` | `/api/generate` now enqueues; new `/api/generation/<id>` |
| `frontend/js/main.js` | Submit posts, then navigates to the durable link |
| `frontend/js/responses.js` | Waiting screen + polling instead of `sessionStorage` |
| `supabase/migrations/002_job_queue.sql` | `generation_jobs` + claim/finish/purge RPCs |
| `.github/workflows/worker.yml` | The worker itself |

## Install

1. Run `supabase/migrations/002_job_queue.sql` in the Supabase SQL editor.
2. **GitHub → Settings → Secrets and variables → Actions**
   * Secrets: `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`, `GEMINI_API_KEYS`
   * Variable (optional): `GEMINI_VISUALIZATION_MODELS`
3. **Vercel → Environment Variables**
   * `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`
   * `GITHUB_REPOSITORY` = `your-username/your-repo`
   * `GITHUB_DISPATCH_TOKEN` = fine-grained PAT, this repo only,
     **Contents: read and write** (that's the permission that allows dispatch)
4. Optional: add a cron calling `purge_old_generation_jobs` to drop old HTML.

## About the 429 you keep seeing

`429 RESOURCE_EXHAUSTED` on the *first* call of *all 17 keys* is not "you used too
much". Two causes match your log:

1. **The model has no free-tier allowance.** Then your free quota for it is zero and
   the first request already returns RESOURCE_EXHAUSTED. Rotating keys cannot help.
2. **Keys from one Google project share one quota.** 17 keys created in the same AI
   Studio / Cloud project behave exactly like one key. Separate *projects* (and
   ideally separate Google accounts) are what actually give separate quotas.

`backend/gemini.py` now handles both: it rotates keys (with a random start so key 1
isn't always burned first), honours Google's `retryDelay`, skips a model instantly on
404, and when **every** key reports exhausted for a model it falls back to the next
model in the chain instead of failing the job.

Chain default, best first (you said 3.6 Flash gives the best output, so it stays
first — the rest only run when 3.6 is truly unavailable):

```
GEMINI_VISUALIZATION_MODELS=gemini-3.6-flash,gemini-flash-latest,gemini-2.5-flash
```

Confirm what your key can actually reach, and put a confirmed model last:

```bash
curl -s "https://generativelanguage.googleapis.com/v1beta/models?key=$YOUR_KEY" \
  | python -c "import json,sys;[print(m['name']) for m in json.load(sys.stdin)['models']]"
```

## Test locally

```bash
export SUPABASE_URL=... SUPABASE_SERVICE_ROLE_KEY=... GEMINI_API_KEYS=...
python -m flask --app backend.app run -p 8000   # submit a prompt in the browser
python worker.py --drain                        # watch it pick the job up
```
