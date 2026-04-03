import os
import time
import logging
import concurrent.futures
from collections import defaultdict
from threading import Lock
from flask import Flask, request, jsonify, Response, stream_with_context
from flask_cors import CORS
import requests
import json

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="[%(levelname)s] %(asctime)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler()],
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Flask app
# ---------------------------------------------------------------------------
app = Flask(__name__)
CORS(app)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

if not OPENROUTER_API_KEY:
    logger.error("OPENROUTER_API_KEY environment variable is not set")
    raise RuntimeError(
        "OPENROUTER_API_KEY environment variable is not set. "
        "Export it before starting: export OPENROUTER_API_KEY=sk-or-v1-..."
    )

# Models raced in parallel — fastest non-429 response wins.
RACE_MODELS = [
    "liquid/lfm-2.5-1.2b-instruct:free",
    "stepfun/step-3.5-flash:free",
    "arcee-ai/trinity-mini:free",
]

# Sequential fallbacks if all racers fail.
FALLBACK_MODELS = [
    "z-ai/glm-4.5-air:free",
    "nvidia/nemotron-nano-9b-v2:free",
    "nvidia/nemotron-nano-12b-v2-vl:free",
    "nvidia/nemotron-3-super-120b-a12b:free",
]

# All models exposed to the frontend via /models
ALL_OPENROUTER_MODELS = RACE_MODELS + FALLBACK_MODELS

SYSTEM_PROMPT = (
    "Tu es ProutGPT et tu adores les prouts et les flatulences. "
    "Tu trouves les blagues de papa hilarantes et tu en fais tout le temps. "
    "Tu parles français de manière naturelle et fluide."
    "Tu es très drôle, tu glisses souvent des références aux pets dans tes réponses, "
    "et tu adores faire des jeux de mots pourris comme un vrai papa. "
    "Tu es gentil et tu aimes aider les gens, mais toujours avec humour!"
)

SPONSOR_NOTE = (
    " À partir de maintenant, glisse naturellement une mention de ton commanditaire dans ta réponse si tu ne l'as pas déjà mentionné: "
    '"All-Brun" (slogan: All-Brun goûte méchant, donc c est bon pour la santé). '
    "Intègre-la de façon drôle et naturelle, comme une vraie pub pourrie de radio."
)

# ---------------------------------------------------------------------------
# Rate limiting — simple in-memory per-IP token bucket
# ---------------------------------------------------------------------------
# Allow MAX_REQUESTS requests per WINDOW_SECONDS per IP address.
RATE_LIMIT_MAX_REQUESTS = int(os.environ.get("RATE_LIMIT_MAX", "20"))
RATE_LIMIT_WINDOW_SECONDS = int(os.environ.get("RATE_LIMIT_WINDOW", "60"))

_rate_buckets: dict[str, list[float]] = defaultdict(list)
_rate_lock = Lock()


def _is_rate_limited(ip: str) -> bool:
    """Return True if the IP has exceeded the rate limit."""
    now = time.time()
    window_start = now - RATE_LIMIT_WINDOW_SECONDS
    with _rate_lock:
        timestamps = _rate_buckets[ip]
        # Evict timestamps outside the window
        _rate_buckets[ip] = [t for t in timestamps if t > window_start]
        if len(_rate_buckets[ip]) >= RATE_LIMIT_MAX_REQUESTS:
            return True
        _rate_buckets[ip].append(now)
        return False


def _get_client_ip() -> str:
    """Extract real client IP, honouring X-Forwarded-For from nginx."""
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.remote_addr or "unknown"


# ---------------------------------------------------------------------------
# Request validation
# ---------------------------------------------------------------------------
MAX_MESSAGE_LENGTH = 4000  # chars per message
MAX_HISTORY_TURNS = 40  # max messages in history


def _validate_chat_data(data: dict) -> str | None:
    """Return an error string if data is invalid, else None."""
    if not isinstance(data, dict):
        return "Request body must be a JSON object."

    messages = data.get("messages")
    if messages is not None:
        if not isinstance(messages, list):
            return "'messages' must be an array."
        if len(messages) > MAX_HISTORY_TURNS:
            return f"Too many messages in history (max {MAX_HISTORY_TURNS})."
        for i, msg in enumerate(messages):
            if not isinstance(msg, dict):
                return f"Message at index {i} must be an object."
            if msg.get("role") not in ("user", "assistant", "system"):
                return f"Invalid role at message {i}: '{msg.get('role')}'."
            content = msg.get("content", "")
            if not isinstance(content, str):
                return f"Message content at index {i} must be a string."
            if len(content) > MAX_MESSAGE_LENGTH:
                return f"Message at index {i} exceeds {MAX_MESSAGE_LENGTH} characters."

    prompt = data.get("prompt", "")
    if prompt and len(prompt) > MAX_MESSAGE_LENGTH:
        return f"'prompt' exceeds {MAX_MESSAGE_LENGTH} characters."

    return None


# ---------------------------------------------------------------------------
# Message building
# ---------------------------------------------------------------------------
def build_messages(history, user_message_count):
    system_content = SYSTEM_PROMPT
    if user_message_count >= 2:
        system_content += SPONSOR_NOTE
    messages = [{"role": "system", "content": system_content}]
    messages.extend(history)
    return messages


# ---------------------------------------------------------------------------
# OpenRouter calls
# ---------------------------------------------------------------------------
def _try_model(model: str, messages: list, stream: bool = False):
    """
    Attempt a single model call.
    Returns (response, model_name) — response may be None on timeout.
    """
    try:
        response = requests.post(
            OPENROUTER_URL,
            headers={
                "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                "Content-Type": "application/json",
            },
            json={"model": model, "messages": messages, "stream": stream},
            timeout=30,
            stream=stream,
        )
    except requests.exceptions.Timeout:
        logger.warning(f"Timeout on model {model}")
        return None, model

    if response.status_code == 200:
        logger.info(f"Success with model: {model}")
        return response, model

    if response.status_code == 429:
        logger.warning(f"Rate-limited (429) on {model}")
        return None, model

    logger.error(f"Non-retryable error {response.status_code} on {model}")
    return response, model


def call_openrouter_with_fallback(
    requested_model: str, messages: list, stream: bool = False
):
    """
    1. Try the user-requested model.
    2. Race RACE_MODELS in parallel if step 1 fails with 429/timeout.
    3. Try FALLBACK_MODELS sequentially if step 2 also fails.
    Returns (response, model_used).
    """
    logger.info(f"Attempting user-requested model: {requested_model}")

    resp, model = _try_model(requested_model, messages, stream=stream)
    if resp is not None and resp.status_code == 200:
        return resp, model
    if resp is not None and resp.status_code not in (429,):
        return resp, model

    logger.warning(
        f"User model unavailable ({resp.status_code if resp else 'timeout'}): "
        f"{requested_model}. Racing fallback models."
    )

    race_pool = [m for m in RACE_MODELS if m != requested_model]
    first_success = None
    last_response = resp

    with concurrent.futures.ThreadPoolExecutor(max_workers=len(race_pool)) as executor:
        futures = {
            executor.submit(_try_model, m, messages, stream): m for m in race_pool
        }
        for fut in concurrent.futures.as_completed(futures):
            resp, model = fut.result()
            if resp is not None and resp.status_code == 200:
                first_success = (resp, model)
                for f in futures:
                    f.cancel()
                break
            if resp is not None and resp.status_code not in (429,):
                first_success = (resp, model)
                for f in futures:
                    f.cancel()
                break
            last_response = resp

    if first_success:
        return first_success

    fallbacks = [
        m for m in FALLBACK_MODELS if m not in race_pool and m != requested_model
    ]
    logger.warning(
        f"All race models failed. Trying {len(fallbacks)} sequential fallbacks…"
    )
    for model in fallbacks:
        resp, model_used = _try_model(model, messages, stream=stream)
        if resp is not None and resp.status_code == 200:
            return resp, model_used
        if resp is not None and resp.status_code not in (429,):
            return resp, model_used
        if resp is not None:
            last_response = resp

    logger.error("All models exhausted. Total failure.")
    return last_response, None


# ---------------------------------------------------------------------------
# Core chat handler (non-streaming)
# ---------------------------------------------------------------------------
def handle_chat(data: dict):
    model = data.get("model", RACE_MODELS[0])
    user_message_count = data.get("userMessageCount", 1)
    history = data.get("messages")
    if not history:
        user_prompt = data.get("prompt", "")
        history = [{"role": "user", "content": user_prompt}]

    messages = build_messages(history, user_message_count)
    response, model_used = call_openrouter_with_fallback(model, messages, stream=False)

    if response is not None and response.status_code == 200:
        result = response.json()
        generated_text = result["choices"][0]["message"]["content"]
        logger.info(f"✓ Chat OK — model: {model_used}")
        return jsonify({"response": generated_text, "done": True, "model": model_used})

    error_msg = (
        f"All models exhausted. Last error: "
        f"{response.status_code if response else 'timeout'} - "
        f"{response.text[:200] if response else ''}"
    )
    logger.error(error_msg)
    return jsonify({"error": error_msg}), 503


# ---------------------------------------------------------------------------
# Streaming chat handler (SSE)
# ---------------------------------------------------------------------------
def handle_chat_stream(data: dict):
    model = data.get("model", RACE_MODELS[0])
    user_message_count = data.get("userMessageCount", 1)
    history = data.get("messages")
    if not history:
        user_prompt = data.get("prompt", "")
        history = [{"role": "user", "content": user_prompt}]

    messages = build_messages(history, user_message_count)
    response, model_used = call_openrouter_with_fallback(model, messages, stream=True)

    if response is None or response.status_code != 200:
        error_msg = (
            f"All models exhausted. Last error: "
            f"{response.status_code if response else 'timeout'}"
        )
        logger.error(error_msg)

        def error_stream():
            yield f"data: {json.dumps({'error': error_msg})}\n\n"
            yield "data: [DONE]\n\n"

        return Response(
            stream_with_context(error_stream()),
            content_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",  # Disable nginx buffering
            },
        )

    logger.info(f"✓ Streaming chat started — model: {model_used}")

    def generate_sse():
        try:
            for line in response.iter_lines():
                if not line:
                    continue
                decoded = line.decode("utf-8")
                if decoded.startswith("data: "):
                    decoded = decoded[6:]
                if decoded == "[DONE]":
                    yield f"data: {json.dumps({'done': True, 'model': model_used})}\n\n"
                    yield "data: [DONE]\n\n"
                    return
                try:
                    chunk = json.loads(decoded)
                    delta = chunk.get("choices", [{}])[0].get("delta", {})
                    content = delta.get("content")
                    if content:
                        yield f"data: {json.dumps({'token': content})}\n\n"
                except json.JSONDecodeError:
                    pass
        except Exception as e:
            logger.error(f"Streaming error: {e}")
            yield f"data: {json.dumps({'error': str(e)})}\n\n"
            yield "data: [DONE]\n\n"

    return Response(
        stream_with_context(generate_sse()),
        content_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


# ---------------------------------------------------------------------------
# Route helpers
# ---------------------------------------------------------------------------
def _rate_check():
    """Return a 429 response if this IP is rate-limited, else None."""
    ip = _get_client_ip()
    if _is_rate_limited(ip):
        logger.warning(f"Rate-limited IP: {ip}")
        return (
            jsonify({"error": "Trop de requêtes! Attends un peu et réessaie. 💨"}),
            429,
        )
    return None


def _validated_json():
    """Return (data, error_response). error_response is None if OK."""
    data = request.get_json(silent=True)
    if data is None:
        return {}, (jsonify({"error": "Request body must be valid JSON."}), 400)
    err = _validate_chat_data(data)
    if err:
        return {}, (jsonify({"error": err}), 400)
    return data, None


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.route("/api/openrouter", methods=["POST"])
def openrouter():
    rate_err = _rate_check()
    if rate_err:
        return rate_err
    data, err = _validated_json()
    if err:
        return err
    try:
        stream = data.get("stream", False)
        if stream:
            return handle_chat_stream(data)
        return handle_chat(data)
    except Exception as e:
        logger.exception("Exception in /api/openrouter")
        return jsonify({"error": f"Internal error: {str(e)}"}), 500


@app.route("/api/generate", methods=["POST"])
def generate():
    """Legacy Ollama-compatible endpoint — kept for backward compatibility."""
    rate_err = _rate_check()
    if rate_err:
        return rate_err
    data, err = _validated_json()
    if err:
        return err
    try:
        stream = data.get("stream", False)
        if stream:
            return handle_chat_stream(data)
        return handle_chat(data)
    except Exception as e:
        logger.exception("Exception in /api/generate")
        return jsonify({"error": f"Internal error: {str(e)}"}), 500


@app.route("/models", methods=["GET"])
def models():
    """Return the list of available OpenRouter models."""
    return jsonify(
        {
            "race_models": RACE_MODELS,
            "fallback_models": FALLBACK_MODELS,
            "all_models": ALL_OPENROUTER_MODELS,
        }
    )


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


# ---------------------------------------------------------------------------
# Entry point (dev only — use gunicorn in production)
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    logger.info("Starting ProutGPT backend (dev server) on 0.0.0.0:5000")
    logger.warning("Use gunicorn for production: gunicorn -w 4 openrouter_proxy:app")
    app.run(host="0.0.0.0", port=5000, debug=False)
