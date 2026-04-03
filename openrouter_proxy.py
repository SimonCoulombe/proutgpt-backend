import os
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

# All free models to try. The requested model is tried first, then these as fallbacks.
FREE_MODELS_FALLBACK = [
    "nvidia/nemotron-3-super-120b-a12b:free",
    "z-ai/glm-4.5-air:free",
    "stepfun/step-3.5-flash:free",
    "nvidia/nemotron-nano-12b-v2-vl:free",
    "nvidia/nemotron-nano-9b-v2:free",
    "liquid/lfm-2.5-1.2b-instruct:free",
    "arcee-ai/trinity-mini:free",
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


def call_openrouter_with_fallback(requested_model, messages):
    """
    Try the requested model first. On 429 (rate-limited), immediately try each
    fallback model in turn. Returns (response, model_used) on success, or
    raises the last response on total failure.
    """
    # Build ordered list: requested model first, then fallbacks (skipping duplicates)
    models_to_try = [requested_model] + [
        m for m in FREE_MODELS_FALLBACK if m != requested_model
    ]

    last_response = None
    for model in models_to_try:
        print(f"[INFO] Trying model: {model}")
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
            print(f"[WARN] Timeout on model {model}, trying next...")
            continue

        if response.status_code == 200:
            print(f"[INFO] Success with model: {model}")
            return response, model

        if response.status_code == 429:
            print(f"[WARN] 429 rate-limited on {model}, trying next...")
            last_response = response
            continue

        # Any other error (4xx/5xx that isn't 429) — stop and return it
        print(f"[ERROR] Non-retryable error {response.status_code} on {model}")
        return response, model

    # All models exhausted
    return last_response, None


def handle_chat(data):
    """Shared logic for both /api/openrouter and /api/generate endpoints."""
    model = data.get("model", FREE_MODELS_FALLBACK[0])
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
