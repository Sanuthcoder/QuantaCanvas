"""
Environment
-----------
GEMINI_API_KEYS               comma-separated keys (required)
GEMINI_VISUALIZATION_MODELS   comma-separated fallback chain, best first
                              default: gemini-3.6-flash,gemini-flash-latest,gemini-2.5-flash
GEMINI_THINKING_LEVEL         low | medium | high   (default medium)
GEMINI_STREAM                 "0" disables streaming (default: stream)
"""

import os
import random
import re
import time

from dotenv import load_dotenv
from google import genai
from google.genai import types

load_dotenv()

API_KEYS = [k.strip() for k in os.environ.get("GEMINI_API_KEYS", "").split(",") if k.strip()]

MODELS = [
    m.strip()
    for m in os.environ.get(
        "GEMINI_VISUALIZATION_MODELS",
        "gemini-3.6-flash,gemini-3.5-flash",
    ).split(",")
    if m.strip()
]

STREAM = os.environ.get("GEMINI_STREAM", "1") != "0"
VERBOSE_STREAM = os.environ.get("GEMINI_VERBOSE_STREAM", "0") == "1"
THINKING_LEVEL = os.environ.get("GEMINI_THINKING_LEVEL", "medium")
MAX_RETRY_DELAY = float(os.environ.get("GEMINI_MAX_RETRY_DELAY", "20"))

SAFETY_SETTINGS = [
    types.SafetySetting(category="HARM_CATEGORY_HARASSMENT", threshold="BLOCK_LOW_AND_ABOVE"),
    types.SafetySetting(category="HARM_CATEGORY_HATE_SPEECH", threshold="BLOCK_LOW_AND_ABOVE"),
    types.SafetySetting(category="HARM_CATEGORY_SEXUALLY_EXPLICIT", threshold="BLOCK_LOW_AND_ABOVE"),
    types.SafetySetting(category="HARM_CATEGORY_DANGEROUS_CONTENT", threshold="BLOCK_LOW_AND_ABOVE"),
]

CONFIG = types.GenerateContentConfig(
    safety_settings=SAFETY_SETTINGS,
    thinking_config=types.ThinkingConfig(thinking_level=THINKING_LEVEL),
    automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
)


class ContentBlocked(RuntimeError):
    def __init__(self):
        super().__init__(
            "This request was blocked by the safety filters. Please try a different prompt."
        )


class ModelExhausted(RuntimeError):
    """Every key failed on every model in the fallback chain."""


def _is_safety_error(error) -> bool:
    message = str(error).upper()
    return any(
        term in message
        for term in ("SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST", "HATE_SPEECH")
    )


def _status_code(error) -> int | None:
    code = getattr(error, "code", None) or getattr(error, "status_code", None)
    if isinstance(code, int):
        return code
    match = re.search(r"\b(400|401|403|404|429|500|503)\b", str(error))
    return int(match.group(1)) if match else None


def _is_quota_error(error) -> bool:
    return _status_code(error) == 429 or "RESOURCE_EXHAUSTED" in str(error).upper()


def _is_missing_model(error) -> bool:
    text = str(error).upper()
    return _status_code(error) == 404 or "NOT_FOUND" in text or "IS NOT SUPPORTED" in text


def _retry_delay(error) -> float:
    """Google returns e.g. "retryDelay": "7s" on a 429. Respect it, capped."""
    match = re.search(r"retryDelay[\"']?\s*[:=]\s*[\"']?(\d+(?:\.\d+)?)s", str(error))
    if not match:
        return 0.0
    return min(float(match.group(1)), MAX_RETRY_DELAY)


def _rotated_keys() -> list[tuple[int, str]]:
    """Same keys, different starting point each call, so key 1 is not always first."""
    numbered = list(enumerate(API_KEYS, start=1))
    if len(numbered) > 1:
        offset = random.randrange(len(numbered))
        numbered = numbered[offset:] + numbered[:offset]
    return numbered


def _call(model: str, api_key: str, contents: str, stream: bool) -> str:
    client = genai.Client(api_key=api_key)
    if not stream:
        return client.models.generate_content(
            model=model, contents=contents, config=CONFIG
        ).text or ""

    parts: list[str] = []
    for chunk in client.models.generate_content_stream(
        model=model, contents=contents, config=CONFIG
    ):
        if chunk.text:
            parts.append(chunk.text)
            if VERBOSE_STREAM:
                print(chunk.text, end="", flush=True)
            else:
                print(".", end="", flush=True)
    print(flush=True)
    return "".join(parts)


def generate_text(contents: str, models: list[str] | None = None, stream: bool | None = None):
    """Return (text, model_used). Raises ContentBlocked or ModelExhausted."""
    if not API_KEYS:
        raise RuntimeError("GEMINI_API_KEYS is not configured.")

    chain = models or MODELS
    use_stream = STREAM if stream is None else stream
    last_error: Exception | None = None

    for model in chain:
        keys = _rotated_keys()
        print(
            f"[gemini] model={model} keys={len(keys)} stream={use_stream}",
            flush=True,
        )
        quota_failures = 0

        for key_number, api_key in keys:
            try:
                started = time.perf_counter()
                text = _call(model, api_key, contents, use_stream)
                if not text.strip():
                    raise ValueError("empty response")
                print(
                    f"[gemini] {model} ok via key {key_number} "
                    f"({len(text)} chars, {time.perf_counter() - started:.1f}s)",
                    flush=True,
                )
                return text, model
            except Exception as error:  # noqa: BLE001 - classify, then decide
                if _is_safety_error(error):
                    raise ContentBlocked() from error

                last_error = error
                label = f"{type(error).__name__}: {str(error)[:180]}"

                if _is_missing_model(error):
                    print(f"[gemini] {model} unavailable, skipping model - {label}", flush=True)
                    break  # no key can fix a bad model name

                if _is_quota_error(error):
                    quota_failures += 1
                    delay = _retry_delay(error)
                    print(
                        f"[gemini] {model} key {key_number} quota exhausted"
                        + (f", waiting {delay:.0f}s" if delay else ""),
                        flush=True,
                    )
                    if delay:
                        time.sleep(delay)
                    continue

                print(f"[gemini] {model} key {key_number} failed - {label}", flush=True)

        if quota_failures and quota_failures == len(keys):
            print(
                f"[gemini] every key is exhausted for {model} "
                "(likely no free-tier allowance for this model, or all keys live in "
                "the same Google project) - falling back to the next model",
                flush=True,
            )

    raise ModelExhausted(
        "Every Gemini model in the fallback chain refused the request "
        f"({chain}). Last error: {last_error}"
    ) from last_error
