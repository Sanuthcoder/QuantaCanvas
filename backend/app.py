import os
import json
from pathlib import Path

from flask import Flask, Response, jsonify, request, send_from_directory, stream_with_context

try:
    from .api import (
        GenerationLimitReached,
        answer_follow_up,
        generate_visualization,
        stream_follow_up,
    )
except ImportError:
    from api import GenerationLimitReached, answer_follow_up, generate_visualization, stream_follow_up


BASE_DIR = Path(__file__).resolve().parent
FRONTEND_DIR = BASE_DIR.parent / "frontend"

app = Flask(__name__, static_folder=None)


@app.get("/")
def home():
    return send_from_directory(FRONTEND_DIR, "index.html")


@app.get("/responses")
def responses():
    return send_from_directory(FRONTEND_DIR, "responses.html")


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


@app.post("/api/generate")
def generate():
    payload = request.get_json(silent=True) or {}
    prompt = str(payload.get("prompt", "")).strip()
    user_id = str(payload.get("user_id", "anonymous")).strip() or "anonymous"

    print(f"[POST /api/generate] prompt received ({len(prompt)} characters)", flush=True)

    if not prompt:
        return jsonify(error="A prompt is required."), 400

    
    try:
        result = generate_visualization(prompt, user_id)
        return jsonify(result)
    except ValueError as error:
        return jsonify(error="Model output invalid. Try again."), 502
    except GenerationLimitReached as error:
        return jsonify(error=str(error)), 429
    except RuntimeError as error:
        app.logger.exception("Visualization model unavailable")
        return jsonify(error=str(error)), 502
    except Exception:
        app.logger.exception("Visualization generation failed")
        return jsonify(error="The visualization could not be generated. Please try again."), 502


@app.post("/api/chat")
def chat():
    payload = request.get_json(silent=True) or {}
    question = str(payload.get("question", "")).strip()
    context = str(payload.get("context", "")).strip()
    history = payload.get("history", [])

    if not question:
        return jsonify(error="A follow-up question is required."), 400

    try:
        reply = answer_follow_up(question, context, history)
        return jsonify(reply=reply)
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

    if not question:
        return jsonify(error="A follow-up question is required."), 400

    def events():
        try:
            for chunk in stream_follow_up(question, summary, history):
                yield f"data: {json.dumps({'text': chunk})}\n\n"
            yield "data: [DONE]\n\n"
        except Exception as error:
            app.logger.exception("Streaming follow-up failed")
            yield f"event: error\ndata: {json.dumps({'error': str(error)})}\n\n"

    return Response(
        stream_with_context(events()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8000")), debug=False)
