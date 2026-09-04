import os
import json
import uuid
from pathlib import Path

from flask import Flask, Response, jsonify, request, send_from_directory, stream_with_context

try:
    from .api import (
        ContentBlocked,
        GenerationLimitReached,
        ChatGenerationLimitReached,
        answer_follow_up,
        confirm_generation,
        generate_visualization,
        get_remaining_generations,
        purge_old_ip_addresses,
        send_contact_email,
        stream_follow_up,
    )
except ImportError:
    from api import (
        GenerationLimitReached,
        ContentBlocked,
        ChatGenerationLimitReached,
        answer_follow_up,
        confirm_generation,
        generate_visualization,
        get_remaining_generations,
        stream_follow_up,
        purge_old_ip_addresses,
        send_contact_email,
    )

try:
    from .api import _start_usage_event
    from .queue_store import enqueue_job, job_status
    from .dispatch import trigger_worker
except ImportError:
    from api import _start_usage_event
    from queue_store import enqueue_job, job_status
    from dispatch import trigger_worker

BASE_DIR = Path(__file__).resolve().parent
FRONTEND_DIR = BASE_DIR.parent / "frontend"

app = Flask(__name__, static_folder=None)


def _request_user_id(payload):
    """Accept only UUIDs, matching the Supabase `users.user_id` column."""
    try:
        return str(uuid.UUID(str(payload.get("user_id", "")).strip()))
    except (ValueError, AttributeError, TypeError):
        return None


def _client_ip():
    # Vercel supplies this header after replacing any client-provided value.
    forwarded = request.headers.get("x-forwarded-for", "")
    return (forwarded.split(",", 1)[0].strip() if forwarded else request.remote_addr) or None


@app.errorhandler(404)
def not_found(error):
    return send_from_directory(FRONTEND_DIR, "404.html"), 404

@app.get("/")
def home():
    return send_from_directory(FRONTEND_DIR, "index.html")




@app.get("/responses")
def responses():
    return send_from_directory(FRONTEND_DIR, "responses.html")

@app.get("/contact")
def contact():
    return send_from_directory(FRONTEND_DIR, "contact.html")

@app.get("/favicon.ico")
def favicon():
    return send_from_directory(FRONTEND_DIR / "assets", "favicon.ico", mimetype="image/x-icon")


@app.get("/<path:filename>")
def frontend_file(filename):
    candidate = FRONTEND_DIR / filename
    if candidate.is_file():
        return send_from_directory(FRONTEND_DIR, filename)

    html_candidate = FRONTEND_DIR / f"{filename}.html"
    if html_candidate.is_file():
        return send_from_directory(FRONTEND_DIR, f"{filename}.html")

    return send_from_directory(FRONTEND_DIR, filename)

@app.get("/api/cron/cleanup_ips")
def cleanup_ips():

    cron_secret = os.environ.get("CRON_SECRET", "")
    auth_header = request.headers.get("Authorization","")
    if not cron_secret or auth_header != f"Bearer {cron_secret}":
        return jsonify(error="Unauthorized"), 401

    purged = purge_old_ip_addresses(older_than_days=3)
    print(f"[cron] cleared ip_address on {purged} generation(s)", flush=True)
    return jsonify(purged=purged)


@app.post("/api/generate")
def generate():
    """Enqueue the job and return immediately.

    The slow Gemini call now happens in the GitHub Actions worker, so this handler
    always finishes in well under a second and Vercel's 300s limit is irrelevant.
    """
    payload = request.get_json(silent=True) or {}
    prompt = str(payload.get("prompt", "")).strip()
    user_id = _request_user_id(payload)

    print(f"[POST /api/generate] prompt received ({len(prompt)} characters)", flush=True)

    if not prompt:
        return jsonify(error="A prompt is required."), 400
    if len(prompt) > 2000:
        return jsonify(error="That prompt is too long. Please shorten it."), 400
    if not user_id:
        return jsonify(error="A valid user identifier is required."), 400

    try:
        # Reserves one of today's generations and creates the pending audit row.
        generation_id = _start_usage_event(user_id, "visualization", prompt, _client_ip())
    except GenerationLimitReached as error:
        return jsonify(error=str(error)), 429
    except Exception:
        app.logger.exception("Could not reserve a generation")
        return jsonify(error="Unable to start a generation right now."), 502

    try:
        enqueue_job(generation_id, user_id, prompt)
    except Exception:
        app.logger.exception("Could not enqueue the generation")
        return jsonify(error="Unable to queue your request. Please try again."), 502

    dispatched = trigger_worker(generation_id)
    print(f"[POST /api/generate] queued {generation_id} (dispatched={dispatched})", flush=True)

    return jsonify(
        job_id=generation_id,
        generation_id=generation_id,
        status="queued",
        dispatched=dispatched,
    ), 202


@app.get("/api/generation/<job_id>")
def generation_state(job_id):
    """Polled by the responses page until the worker finishes the job."""
    try:
        job_id = str(uuid.UUID(job_id))
    except (ValueError, AttributeError, TypeError):
        return jsonify(error="A valid job id is required."), 400

    try:
        row = job_status(job_id)
    except Exception:
        app.logger.exception("Job status lookup failed")
        return jsonify(error="Unable to check the status right now."), 502

    if not row:
        return jsonify(error="That generation could not be found."), 404

    body = {
        "job_id": row["id"],
        "status": row["status"],
        "prompt": row.get("prompt", ""),
        "attempts": row.get("attempts", 0),
        "generation_time_ms": row.get("generation_time_ms"),
    }

    if row["status"] == "completed":
        body["html"] = row.get("result_html") or ""
        body["summary"] = row.get("result_summary") or ""
        if not body["html"]:
            body["status"] = "expired"
            body["error"] = "This visualization has expired. Please generate it again."
    elif row["status"] == "failed":
        body["error"] = row.get("error_message") or "The visualization could not be generated."

    return jsonify(body)


@app.post("/api/chat")
def chat():
    payload = request.get_json(silent=True) or {}
    question = str(payload.get("question", "")).strip()
    context = str(payload.get("context", "")).strip()
    history = payload.get("history", [])
    user_id = _request_user_id(payload)

    if not question:
        return jsonify(error="A follow-up question is required."), 400
    if not user_id:
        return jsonify(error="A valid user identifier is required."), 400

    try:
        reply = answer_follow_up(question, context, history, user_id, _client_ip())
        return jsonify(reply=reply)
    except (GenerationLimitReached, ChatGenerationLimitReached) as error:
        return jsonify(error=str(error)), 429
    except ContentBlocked as error:
        return jsonify(error=str(error)), 403
    except RuntimeError as error:
        app.logger.exception("Chat model unavailable")
        return jsonify(error=str(error)), 502
    except Exception:
        app.logger.exception("Follow-up generation failed")
        return jsonify(error="The follow-up answer could not be generated."), 502


@app.post("/api/chat/stream")
def chat_stream():
    payload = request.get_json(silent=True) or {}
    question = str(payload.get("question", "")).strip()
    summary = str(payload.get("summary", "")).strip()
    history = payload.get("history", [])
    user_id = _request_user_id(payload)

    if not question:
        return jsonify(error="A follow-up question is required."), 400
    if not user_id:
        return jsonify(error="A valid user identifier is required."), 400

    def events():
        try:
            for chunk in stream_follow_up(question, summary, history, user_id, _client_ip()):
                yield f"data: {json.dumps({'text': chunk})}\n\n"
            yield "data: [DONE]\n\n"
        except (ContentBlocked, GenerationLimitReached, ChatGenerationLimitReached) as error:
            status = 403 if isinstance(error, ContentBlocked) else 429
            yield f"event: error\ndata: {json.dumps({'error': str(error), 'status': status})}\n\n"
        except Exception as error:
            app.logger.exception("Streaming follow-up failed")
            yield f"event: error\ndata: {json.dumps({'error': str(error)})}\n\n"

    return Response(
        stream_with_context(events()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/contact")
def api_contact():
    payload = request.get_json(silent=True) or {}
    name = str(payload.get("name", "")).strip()
    email = str(payload.get("email", "")).strip()
    message = str(payload.get("message", "")).strip()
    user_id = str(payload.get("user_id", "anonymous")).strip() or "anonymous"

    if not name or not email or not message:
        return jsonify(error="Name, email, and message are all required."), 400

    try:
        send_contact_email(name, email, message, user_id)
        return jsonify(ok=True)
    except Exception as error:
        app.logger.exception("Contact email failed")
        return jsonify(error=str(error)), 502


@app.post("/api/generation/confirm")
def api_confirm_generation():
    payload = request.get_json(silent=True) or {}
    generation_id = str(payload.get("generation_id", "")).strip()
    user_id = str(payload.get("user_id", "")).strip() or None
    prompt = str(payload.get("prompt", "")).strip()
    try:
        generation_id = str(uuid.UUID(generation_id))
    except (ValueError, AttributeError, TypeError):
        return jsonify(error="A valid generation_id is required."), 400
    confirmed = confirm_generation(generation_id, user_id=user_id, prompt=prompt, ip_address=_client_ip())
    return jsonify(confirmed=confirmed), 200 if confirmed else 404


@app.get("/api/generations/remaining")
def api_generations_remaining():
    user_id = _request_user_id({"user_id": request.args.get("user_id", "")})
    if not user_id:
        return jsonify(error="A valid user identifier is required."), 400
    return jsonify(get_remaining_generations(user_id))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8000")), debug=False)
