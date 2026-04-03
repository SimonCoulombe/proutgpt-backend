import os
import concurrent.futures
from flask import Flask, request, jsonify
from flask_cors import CORS
import requests

app = Flask(__name__)
CORS(app)

OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

if not OPENROUTER_API_KEY:
    raise RuntimeError(
        "OPENROUTER_API_KEY environment variable is not set. "
        "Export it before starting: export OPENROUTER_API_KEY=sk-or-v1-..."
    )

# These 3 are raced in parallel on every request — fastest non-429 wins.
# All are small/fast models with good availability on free tier.
RACE_MODELS = [
    "liquid/lfm-2.5-1.2b-instruct:free",
    "stepfun/step-3.5-flash:free",
    "arcee-ai/trinity-mini:free",
]

# Sequential fallbacks if all 3 racers fail (429 or timeout).
FALLBACK_MODELS = [
    "z-ai/glm-4.5-air:free",
    "nvidia/nemotron-nano-9b-v2:free",
    "nvidia/nemotron-nano-12b-v2-vl:free",
    "nvidia/nemotron-3-super-120b-a12b:free",
]

SYSTEM_PROMPT = (
    "Tu es ProutGPT et tu adores les prouts et les flatulences. "
    "Tu trouves les blagues de papa hilarantes et tu en fais tout le temps. "
    "Tu parles français de manière naturelle et fluide car tu es Québécois (mais tu ne mentionnes pas ton origine sauf si on te le demande). "
    "Tu es très drôle, tu glisses souvent des références aux pets dans tes réponses, "
    "et tu adores faire des jeux de mots pourris comme un vrai papa. "
    "Tu es gentil et tu aimes aider les gens, mais toujours avec humour!"
)

SPONSOR_NOTE = (
    " À partir de maintenant, glisse naturellement une mention de ton commanditaire dans ta réponse : "
    '"All-Brun" (slogan: All-Brun goûte méchant, donc c est bon pour la santé). '
    "Intègre-la de façon drôle et naturelle, comme une vraie pub pourrie de radio."
)


def build_messages(history, user_message_count):
    """
    Build the messages list for the OpenRouter API.
    `history` is a list of {role, content} dicts (already includes the latest user message).
    The sponsor note is added to the system prompt starting from the 2nd user message.
    """
    system_content = SYSTEM_PROMPT
    if user_message_count >= 2:
        system_content += SPONSOR_NOTE

    messages = [{"role": "system", "content": system_content}]
    messages.extend(history)
    return messages


def _try_model(model, messages):
    """
    Attempt a single model call. Returns (response, model) on HTTP success,
    or raises an exception / returns (None, model) on 429/timeout so the
    race can skip it.
    """
    try:
        response = requests.post(
            OPENROUTER_URL,
            headers={
                "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                "Content-Type": "application/json",
            },
            json={"model": model, "messages": messages},
            timeout=30,
        )
    except requests.exceptions.Timeout:
        print(f"[WARN] Timeout on model {model}")
        return None, model

    if response.status_code == 200:
        print(f"[INFO] Success with model: {model}")
        return response, model

    if response.status_code == 429:
        print(f"[WARN] 429 rate-limited on {model}")
        return None, model

    # Non-retryable error — return it so the caller can surface it
    print(f"[ERROR] Non-retryable error {response.status_code} on {model}")
    return response, model


def call_openrouter_with_fallback(requested_model, messages):
    """
    1. Race RACE_MODELS (plus requested_model if not already in the list) in
       parallel — return the first successful (200) response.
    2. If all racers fail with 429/timeout, try FALLBACK_MODELS sequentially.
    Returns (response, model_used) on success, or (last_response, None) on
    total failure.
    """
    # Build the race pool: requested model first, then the standard racers
    race_pool = [requested_model] + [m for m in RACE_MODELS if m != requested_model]

    print(f"[INFO] Racing {len(race_pool)} models in parallel: {race_pool}")

    first_success = None
    last_response = None

    with concurrent.futures.ThreadPoolExecutor(max_workers=len(race_pool)) as executor:
        futures = {executor.submit(_try_model, m, messages): m for m in race_pool}
        for fut in concurrent.futures.as_completed(futures):
            resp, model = fut.result()
            if resp is not None and resp.status_code == 200:
                first_success = (resp, model)
                # Cancel remaining futures (best-effort; in-flight requests
                # will still complete but we ignore them)
                for f in futures:
                    f.cancel()
                break
            if resp is not None and resp.status_code not in (429,):
                # Non-retryable error from one of the racers — surface it
                first_success = (resp, model)
                for f in futures:
                    f.cancel()
                break
            last_response = resp  # track last 429 response for error reporting

    if first_success:
        return first_success

    # All racers failed — try sequential fallbacks
    fallbacks = [m for m in FALLBACK_MODELS if m not in race_pool]
    print(f"[WARN] All racers failed. Trying {len(fallbacks)} sequential fallbacks...")
    for model in fallbacks:
        print(f"[INFO] Trying fallback model: {model}")
        resp, model_used = _try_model(model, messages)
        if resp is not None and resp.status_code == 200:
            return resp, model_used
        if resp is not None and resp.status_code not in (429,):
            return resp, model_used
        if resp is not None:
            last_response = resp

    return last_response, None


def handle_chat(data):
    """Shared logic for both /api/openrouter and /api/generate endpoints."""
    model = data.get("model", RACE_MODELS[0])
    user_message_count = data.get("userMessageCount", 1)

    # Accept full conversation history; fall back to single prompt for old clients
    history = data.get("messages")
    if not history:
        user_prompt = data.get("prompt", "")
        history = [{"role": "user", "content": user_prompt}]

    messages = build_messages(history, user_message_count)
    response, model_used = call_openrouter_with_fallback(model, messages)

    if response is not None and response.status_code == 200:
        result = response.json()
        generated_text = result["choices"][0]["message"]["content"]
        return jsonify({"response": generated_text, "done": True, "model": model_used})

    error_msg = f"All models exhausted or failed. Last error: {response.status_code if response else 'timeout'} - {response.text[:200] if response else ''}"
    print(f"[ERROR] {error_msg}")
    return jsonify({"error": error_msg}), 503


@app.route("/api/openrouter", methods=["POST"])
def openrouter():
    try:
        return handle_chat(request.json)
    except Exception as e:
        error_msg = f"Exception in /api/openrouter: {str(e)}"
        print(f"[ERROR] {error_msg}")
        return jsonify({"error": error_msg}), 500


@app.route("/api/generate", methods=["POST"])
def generate():
    """
    Legacy Ollama-compatible endpoint — kept for backward compatibility.
    Forwards to OpenRouter using the same model logic.
    """
    try:
        return handle_chat(request.json)
    except Exception as e:
        error_msg = f"Exception in /api/generate: {str(e)}"
        print(f"[ERROR] {error_msg}")
        return jsonify({"error": error_msg}), 500


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
