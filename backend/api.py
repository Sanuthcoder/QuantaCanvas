import os
import re
import smtplib
import time
import uuid
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv
from google import genai
from google.genai.errors import APIError
from supabase import Client, create_client
from google.genai import types

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
VISUALIZATION_MODEL = os.environ.get("GEMINI_VISUALIZATION_MODEL", "gemini-3.6-flash")
CHAT_MODEL = os.environ.get("GEMINI_CHAT_MODEL", "gemma-4-26b-a4b-it")

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").strip()
SUPABASE_SERVICE_ROLE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "").strip()
supabase: Client | None = (
    create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)
    if SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY
    else None
)

GENERATION_LIMIT = int(os.environ.get("DAILY_GENERATION_LIMIT", "7"))
CHAT_GENERATION_LIMIT = int(os.environ.get("DAILY_CHAT_GENERATION_LIMIT", "50"))
USAGE_TIMEZONE_NAME = os.environ.get("USAGE_TIMEZONE", "UTC")
try:
    USAGE_TIMEZONE = ZoneInfo(USAGE_TIMEZONE_NAME)
except ZoneInfoNotFoundError as error:
    
    if USAGE_TIMEZONE_NAME == "UTC":
        USAGE_TIMEZONE = timezone.utc
    else:
        raise RuntimeError(
            f"USAGE_TIMEZONE is not a valid IANA timezone: {USAGE_TIMEZONE_NAME}. "
            "Install the tzdata package for non-system timezones."
        ) from error

CONTACT_DESTINATION = os.environ.get("CONTACT_EMAIL", "aistudyhub4@gmail.com")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "").strip()


SAFETY_SETTINGS = [
    types.SafetySetting(category="HARM_CATEGORY_HARASSMENT", threshold="BLOCK_LOW_AND_ABOVE"),
    types.SafetySetting(category="HARM_CATEGORY_HATE_SPEECH", threshold="BLOCK_LOW_AND_ABOVE"),
    types.SafetySetting(category="HARM_CATEGORY_SEXUALLY_EXPLICIT", threshold="BLOCK_LOW_AND_ABOVE"),
    types.SafetySetting(category="HARM_CATEGORY_DANGEROUS_CONTENT", threshold="BLOCK_LOW_AND_ABOVE"),
]
GENERATION_CONFIG = types.GenerateContentConfig(
    safety_settings=SAFETY_SETTINGS,
    thinking_config=types.ThinkingConfig(thinking_level="medium"),
)

class DailyLimitReached(RuntimeError):
    def __init__(self, event_type, reset_at):
        limit = GENERATION_LIMIT if event_type == "visualization" else CHAT_GENERATION_LIMIT
        label = "visualizations" if event_type == "visualization" else "follow-up messages"
        super().__init__(
            f"You have reached the daily limit of {limit} {label}. "
            f"Please try again after {reset_at.strftime('%Y-%m-%d %H:%M %Z')}."
        )
        self.reset_at = reset_at

GenerationLimitReached = DailyLimitReached
ChatGenerationLimitReached = DailyLimitReached


class ContentBlocked(RuntimeError):
    def __init__(self):
        super().__init__(
            "This request was blocked by the safety filters. Please try a different prompt."
        )

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


def _ensure_user(user_id):
    if not supabase or not user_id:
        raise RuntimeError("Supabase is not configured or user ID is missing.")

    try:
        supabase.table("users").upsert(
            {"user_id": user_id},
            on_conflict="user_id",
        ).execute()
    except Exception as error:
        print(f"[Supabase] user creation failed: {error}", flush=True)
        raise RuntimeError("Unable to register user.") from error

def _start_usage_event(user_id, event_type, prompt, ip_address):
    """Atomically reserve one daily request and create its pending audit row.

    The `start_usage_event` RPC is installed by supabase/migrations/001_usage.sql.
    It counts pending + completed requests so concurrent requests cannot exceed a
    limit; failed requests are not counted and therefore release their slot.
    """
    _ensure_user(user_id)
    if not supabase:
        raise RuntimeError("Supabase is not configured.")
    try:
        result = supabase.rpc(
            "start_usage_event",
            {
                "p_user_id": user_id,
                "p_event_type": event_type,
                "p_prompt": prompt,
                "p_ip_address": ip_address,
                "p_daily_limit": GENERATION_LIMIT if event_type == "visualization" else CHAT_GENERATION_LIMIT,
                "p_timezone": USAGE_TIMEZONE_NAME,
            },
        ).execute()
        row = result.data[0] if result.data else None
        if not row:
            raise RuntimeError("Supabase did not return a usage event.")
        if not row["allowed"]:
            tomorrow = datetime.now(USAGE_TIMEZONE).replace(
                hour=0, minute=0, second=0, microsecond=0
            ) + timedelta(days=1)
            raise DailyLimitReached(event_type, tomorrow)
        return row["generation_id"]
    except Exception as error:
        if isinstance(error, DailyLimitReached):
            raise
        raise RuntimeError("Unable to record usage right now.") from error


def _count_completed_generation_rows(user_id, event_type):
    if not supabase or not user_id:
        return 0
    day_start = datetime.now(USAGE_TIMEZONE).replace(
        hour=0, minute=0, second=0, microsecond=0
    ).astimezone(timezone.utc)
    try:
        result = (
            supabase.table("generations")
            .select("id", count="exact")
            .eq("user_id", user_id)
            .eq("event_type", event_type)
            .eq("status", "completed")
            .gte("created_at", day_start.isoformat())
            .execute()
        )
        return result.count or 0
    except Exception as error:
        print(f"[Supabase] completed generation count failed: {error}", flush=True)
        return 0


def _ensure_visualization_limit(user_id):
    used = _count_completed_generation_rows(user_id, "visualization")
    if used >= GENERATION_LIMIT:
        tomorrow = datetime.now(USAGE_TIMEZONE).replace(
            hour=0, minute=0, second=0, microsecond=0
        ) + timedelta(days=1)
        raise DailyLimitReached("visualization", tomorrow)


def _register_generation(user_id, event_type, prompt, ip_address, status="pending"):
    if not supabase or not user_id:
        return None
    generation_id = str(uuid.uuid4())
    try:
        supabase.table("generations").insert(
            {
                "id": generation_id,
                "user_id": user_id,
                "event_type": event_type,
                "status": status,
                "prompt": prompt,
                "ip_address": ip_address,
            }
        ).execute()
        return generation_id
    except Exception as error:
        print(f"[Supabase] generation registration failed: {error}", flush=True)
        return None


def _finish_generation(generation_id, status, started):
    if not supabase or not generation_id:
        return 
    try:
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        supabase.table("generations").update(
            {
                "status": status,
                "generation_time_ms": elapsed_ms,
            }
        ).eq("id", generation_id).execute()
    except Exception as error:
        print(f"[Supabase] generation completion failed: {error}", flush=True)


def _user_friendly_model_error():
    return RuntimeError("The model is currently exhausted. Please try again in a moment.")


def _is_safety_error(error):
    message = str(error).upper()
    return any(term in message for term in ("SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST", "HATE_SPEECH"))


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
                return client.models.generate_content(
                    model=model, contents=contents, config=GENERATION_CONFIG
                ).text or ""

            response_text = []
            print(f"[{model}] visualization stream started", flush=True)
            for chunk in client.models.generate_content_stream(
                model=model, contents=contents, config=GENERATION_CONFIG
            ):
                text = chunk.text or ""
                response_text.append(text)
                print(text, end="", flush=True)
            print("\n[visualization stream complete]\n", flush=True)
            return "".join(response_text)
        except Exception as error:
            if _is_safety_error(error):
                raise ContentBlocked() from error
            last_error = error
            print(
                f"\n[{model}] key {key_number}/{len(API_KEYS)} failed: "
                f"{type(error).__name__}: {error}",
                flush=True,
            )
    raise _user_friendly_model_error() from last_error


def _clean_document(content):
    match = re.search(r"<!DOCTYPE html>.*</html>", content, re.DOTALL | re.IGNORECASE)
    if not match:
        raise ValueError("Model output did not contain a complete HTML document.")
    document = match.group(0).strip().replace("```html", "").replace("```", "")
    if "QuantaCanvas KaTeX recovery" not in document and "</head>" in document.lower():
        head_end = re.search(r"</head\s*>", document, re.IGNORECASE)
        document = document[:head_end.start()] + KATEX_RECOVERY + document[head_end.start():]
    return document


def generate_visualization(user_prompt, user_id, ip_address):
    if user_id:
        _ensure_visualization_limit(user_id)
    started = time.perf_counter()
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
        generation_id = str(uuid.uuid4())
        return {
            "html": html,
            "summary": summary_match.group(1).strip() if summary_match else "",
            "generation_time_ms": int((time.perf_counter() - started) * 1000),
            "context": f"USER QUESTION: {user_prompt}",
            "generation_id": generation_id,
        }
    except Exception:
        raise


def answer_follow_up(question, context, history, user_id, ip_address):
    started = time.perf_counter()
    generation_id = _start_usage_event(user_id, "follow_up", question, ip_address)
    transcript = _chat_transcript(history)
    try:
        reply = _call_model(_chat_prompt(question, context, transcript), CHAT_MODEL)
        _finish_generation(generation_id, "completed", started)
        return reply
    except Exception:
        _finish_generation(generation_id, "failed", started)
        raise


def _chat_transcript(history):
    return "\n".join(
        f"{item.get('role', 'user').upper()}: {item.get('text', '')}"
        for item in history[-12:]
        if isinstance(item, dict) and item.get("text")
    )


def _chat_prompt(question, summary, transcript):
    return f"{FOLLOW_UP_INSTRUCTIONS}\nLESSON SUMMARY (not the full visualization):\n{summary}\nHISTORY:\n{transcript}\nQUESTION:\n{question}"


def stream_follow_up(question, summary, history, user_id, ip_address):
    """Yield follow-up text chunks using the original prompt configuration."""
    started = time.perf_counter()
    generation_id = _start_usage_event(user_id, "follow_up", question, ip_address)
    completed = False
    try:
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
                for chunk in client.models.generate_content_stream(
                    model=CHAT_MODEL, contents=prompt, config=GENERATION_CONFIG
                ):
                    if chunk.text:
                        yield chunk.text
                completed = True
                return
            except Exception as error:
                if _is_safety_error(error):
                    raise ContentBlocked() from error
                last_error = error
                print(
                    f"[Gemini chat] key {key_number}/{len(API_KEYS)} failed: "
                    f"{type(error).__name__}: {error}",
                    flush=True,
                )
        raise _user_friendly_model_error() from last_error
    finally:
        _finish_generation(generation_id, "completed" if completed else "failed", started)


def confirm_generation(generation_id, user_id=None, prompt="", ip_address=None):
    """Register a visualization only after the responses page confirms it loaded."""
    if not supabase or not generation_id:
        return False
    try:
        existing = (
            supabase.table("generations")
            .select("id", "status")
            .eq("id", generation_id)
            .eq("event_type", "visualization")
            .execute()
        )
        if existing.data:
            row = existing.data[0]
            if row.get("status") == "completed":
                return True
            if row.get("status") == "failed":
                return False
            result = (
                supabase.table("generations")
                .update({"status": "completed"})
                .eq("id", generation_id)
                .eq("event_type", "visualization")
                .eq("status", "pending")
                .execute()
            )
            return bool(result.data)

        if user_id:
            supabase.table("generations").insert(
                {
                    "id": generation_id,
                    "user_id": user_id,
                    "event_type": "visualization",
                    "status": "completed",
                    "prompt": prompt,
                    "ip_address": ip_address,
                }
            ).execute()
            return True
        return False
    except Exception as error:
        print(f"[Supabase] generation confirmation failed: {error}", flush=True)
        return False


def get_remaining_generations(user_id):
    if not supabase:
        return {"used": 0, "limit": GENERATION_LIMIT, "remaining": GENERATION_LIMIT}
    day_start = datetime.now(USAGE_TIMEZONE).replace(
        hour=0, minute=0, second=0, microsecond=0
    ).astimezone(timezone.utc)
    try:
        result = (
            supabase.table("generations")
            .select("id", count="exact")
            .eq("user_id", user_id)
            .eq("event_type", "visualization")
            .eq("status", "completed")
            .gte("created_at", day_start.isoformat())
            .execute()
        )
        used = result.count or 0
        return {"used": used, "limit": GENERATION_LIMIT, "remaining": max(0, GENERATION_LIMIT - used)}
    except Exception as error:
        print(f"[Supabase] remaining generation lookup failed: {error}", flush=True)
        return {"used": 0, "limit": GENERATION_LIMIT, "remaining": GENERATION_LIMIT}


def purge_old_ip_addresses(older_than_days=3):
    if not supabase:
        return 0
    cutoff = (datetime.now(timezone.utc) - timedelta(days=older_than_days)).isoformat()
    try:
        result = supabase.table("generations").update({"ip_address": None}).lt(
            "created_at", cutoff
        ).execute()
        return len(result.data or [])
    except Exception as error:
        print(f"[Supabase] IP purge failed: {error}", flush=True)
        return 0


def send_contact_email(name, sender_email, message, user_id):
    if not SMTP_PASSWORD:
        raise RuntimeError("Email sending is not configured. Set SMTP_PASSWORD.")
    email = MIMEMultipart()
    email["From"] = CONTACT_DESTINATION
    email["To"] = CONTACT_DESTINATION
    email["Subject"] = f"QuantaCanvas Contact: {name}"
    email["Reply-To"] = sender_email
    email.attach(MIMEText(
        f"Name: {name}\nEmail: {sender_email}\nUser ID: {user_id}\n\n{message}", "plain"
    ))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(CONTACT_DESTINATION, SMTP_PASSWORD)
        server.sendmail(CONTACT_DESTINATION, CONTACT_DESTINATION, email.as_string())
