import os
import json
import re
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv
from google import genai
from google.genai.errors import APIError
from supabase import Client, create_client

try:
    from .prompt import PROMPT
except ImportError:
    from prompt import PROMPT

try:
    from .prompt import FOLLOW_UP_INSTRUCTIONS
except ImportError:
    from prompt import FOLLOW_UP_INSTRUCTIONS


load_dotenv()

API_KEYS = [key.strip() for key in os.environ.get("GEMINI_API_KEYS", "").split(",") if key.strip()]
VISUALIZATION_MODEL = os.environ.get("GEMINI_VISUALIZATION_MODEL", "gemini-3.5-flash")
CHAT_MODEL = os.environ.get("GEMINI_CHAT_MODEL", "gemma-4-26b-a4b-it")

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").strip()
SUPABASE_SERVICE_ROLE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "").strip()
supabase: Client | None = (
    create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)
    if SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY
    else None
)

GENERATION_LIMIT = int(os.environ.get("DAILY_GENERATION_LIMIT", "7"))


class GenerationLimitReached(RuntimeError):
    def __init__(self, reset_at):
        super().__init__(
            f"You have reached the limit of {GENERATION_LIMIT} visualizations "
            "in the last 24 hours. Please try again after "
            f"{reset_at.strftime('%Y-%m-%d %H:%M UTC')}."
        )
        self.reset_at = reset_at

KATEX_RECOVERY = r"""
<script>
(function () {
    const options = {
        delimiters: [
            { left: '$$', right: '$$', display: true },
            { left: '$', right: '$', display: false },
            { left: '\\(', right: '\\)', display: false },
            { left: '\\[', right: '\\]', display: true }
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


def _require_keys():
    if not API_KEYS:
        raise RuntimeError("GEMINI_API_KEYS is not configured.")


def _register_user(user_id):
    if not supabase or not user_id or user_id == "anonymous":
        return
    try:
        supabase.table("users").upsert(
            {"user_id": user_id, "last_seen": "now()"},
            on_conflict="user_id",
        ).execute()
    except Exception as error:
        print(f"[Supabase] user analytics failed: {error}", flush=True)


def _create_generation(user_id):
    if not supabase:
        return None


def _enforce_generation_limit(user_id):
    if not supabase:
        return

    window_start = datetime.now(timezone.utc) - timedelta(days=1)
    try:
        result = (
            supabase.table("generations")
            .select("id, created_at", count="exact")
            .eq("user_id", user_id or "anonymous")
            .gte("created_at", window_start.isoformat())
            .order("created_at")
            .execute()
        )
        generation_count = result.count or 0
        if generation_count >= GENERATION_LIMIT:
            oldest = result.data[0].get("created_at") if result.data else None
            reset_at = window_start + timedelta(days=1)
            if oldest:
                reset_at = datetime.fromisoformat(oldest.replace("Z", "+00:00")) + timedelta(days=1)
            raise GenerationLimitReached(reset_at)
    except GenerationLimitReached:
        raise
    except Exception as error:
        print(f"[Supabase] generation limit check failed: {error}", flush=True)
    generation_id = str(uuid.uuid4())
    try:
        supabase.table("generations").insert(
            {"id": generation_id, "user_id": user_id or "anonymous", "status": "pending"}
        ).execute()
        return generation_id
    except Exception as error:
        print(f"[Supabase] generation start failed: {error}", flush=True)
        return None


def _finish_generation(generation_id, status, started):
    if not supabase or not generation_id:
        return
    try:
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        supabase.table("generations").update(
            {
                "status": status,
                "completed_at": "now()",
                "generation_time_ms": elapsed_ms,
            }
        ).eq("id", generation_id).execute()
    except Exception as error:
        print(f"[Supabase] generation completion failed: {error}", flush=True)


def _call_model(contents, model, stream_output=False):
    _require_keys()
    last_error = None
    print(
        f"[Gemini] starting request: model={model}, keys_available={len(API_KEYS)}, "
        f"stream={stream_output}",
        flush=True,
    )
    for key_number, api_key in enumerate(API_KEYS, start=1):
        try:
            client = genai.Client(api_key=api_key)
            if not stream_output:
                return client.models.generate_content(model=model, contents=contents).text or ""

            response_text = []
            print(f"[{model}] visualization stream started", flush=True)
            for chunk in client.models.generate_content_stream(model=model, contents=contents):
                text = chunk.text or ""
                response_text.append(text)
                print(text, end="", flush=True)
            print("\n[visualization stream complete]\n", flush=True)
            return "".join(response_text)
        except Exception as error:
            last_error = error
            print(
                f"\n[{model}] key {key_number}/{len(API_KEYS)} failed: "
                f"{type(error).__name__}: {error}",
                flush=True,
            )
    raise RuntimeError(
        f"Gemini request failed for {len(API_KEYS)} configured key(s): "
        f"{type(last_error).__name__}: {last_error}"
    ) from last_error


def _clean_document(content):
    match = re.search(r"<!DOCTYPE html>.*</html>", content, re.DOTALL | re.IGNORECASE)
    if not match:
        raise ValueError("Model output did not contain a complete HTML document.")
    document = match.group(0).strip().replace("```html", "").replace("```", "")
    if "QuantaCanvas KaTeX recovery" not in document and "</head>" in document.lower():
        head_end = re.search(r"</head\s*>", document, re.IGNORECASE)
        document = document[:head_end.start()] + KATEX_RECOVERY + document[head_end.start():]
    return document


def generate_visualization(user_prompt, user_id, static_dir=None):
    started = time.perf_counter()
    _register_user(user_id)
    _enforce_generation_limit(user_id)
    generation_id = _create_generation(user_id)
    try:
        content = _call_model(
            f"{PROMPT}\n\nUSER REQUEST: {user_prompt}",
            VISUALIZATION_MODEL,
            stream_output=True,
        )
        html = _clean_document(content)
        summary_match = re.search(
            r"<<<LESSON_SUMMARY>>>(.*?)<<<END_LESSON_SUMMARY>>>",
            content,
            re.DOTALL | re.IGNORECASE,
        )
        _finish_generation(generation_id, "completed", started)
        return {
            "html": html,
            "summary": summary_match.group(1).strip() if summary_match else "",
            "generation_time_ms": int((time.perf_counter() - started) * 1000),
            "context": f"USER QUESTION: {user_prompt}",
        }
    except Exception:
        _finish_generation(generation_id, "failed", started)
        raise


def answer_follow_up(question, context, history):
    transcript = _chat_transcript(history)
    return _call_model(_chat_prompt(question, context, transcript), CHAT_MODEL)


def _chat_transcript(history):
    return "\n".join(
        f"{item.get('role', 'user').upper()}: {item.get('text', '')}"
        for item in history[-12:]
        if isinstance(item, dict) and item.get("text")
    )


def _chat_prompt(question, summary, transcript):
    return f"{FOLLOW_UP_INSTRUCTIONS}\nLESSON SUMMARY (not the full visualization):\n{summary}\nHISTORY:\n{transcript}\nQUESTION:\n{question}"


def stream_follow_up(question, summary, history):
    """Yield follow-up text chunks using the original prompt configuration."""
    _require_keys()
    transcript = _chat_transcript(history[:-1])
    prompt = (
        f"{FOLLOW_UP_INSTRUCTIONS}\n\n"
        f"CONTEXT (lesson summary, not the full visualization):\n{summary}\n\n"
        f"CHAT HISTORY:\n{transcript}\n\n"
        f"USER QUESTION and LESSON SUMMARY:\n{question}\n{summary}"
    )
    last_error = None
    for key_number, api_key in enumerate(API_KEYS, start=1):
        try:
            client = genai.Client(api_key=api_key)
            print(
                f"[Gemini chat] streaming response, model={CHAT_MODEL}, key {key_number}/{len(API_KEYS)}, "
                f"summary_chars={len(summary)}",
                flush=True,
            )
            for chunk in client.models.generate_content_stream(model=CHAT_MODEL, contents=prompt):
                if chunk.text:
                    yield chunk.text
            return
        except Exception as error:
            last_error = error
            print(
                f"[Gemini chat] key {key_number}/{len(API_KEYS)} failed: "
                f"{type(error).__name__}: {error}",
                flush=True,
            )
    raise RuntimeError(
        f"Gemini chat failed for {len(API_KEYS)} configured key(s): "
        f"{type(last_error).__name__}: {last_error}"
    ) from last_error
