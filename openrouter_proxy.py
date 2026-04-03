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

SYSTEM_PROMPT = (
    "Tu es ProutGPT et tu adores le fromage et les prouts. "
    "Tu trouves les blagues de papa hilarantes et tu en fais tout le temps. "
    "Tu parles français de manière naturelle et fluide. "
    "Tu es très drôle, tu glisses souvent des références aux pets et au fromage dans tes réponses, "
    "et tu adores faire des jeux de mots pourris comme un vrai papa. "
    "Tu es gentil et tu aimes aider les gens, mais toujours avec humour!"
)

SPONSOR_NOTE = (
    " À partir de maintenant, glisse naturellement une mention de ton commanditaire dans ta réponse : "
    '"All-Brun" (une céréale imaginaire et ridicule). '
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


def call_openrouter(model, messages):
    response = requests.post(
        OPENROUTER_URL,
        headers={
            "Authorization": f"Bearer {OPENROUTER_API_KEY}",
            "Content-Type": "application/json",
        },
        json={"model": model, "messages": messages},
        timeout=60,
    )
    return response


@app.route("/api/openrouter", methods=["POST"])
def openrouter():
    data = request.json
    model = data.get("model", "z-ai/glm-4.5-air:free")
    user_message_count = data.get("userMessageCount", 1)

    # Accept full conversation history; fall back to single prompt for old clients
    history = data.get("messages")
    if not history:
        user_prompt = data.get("prompt", "")
        history = [{"role": "user", "content": user_prompt}]

    messages = build_messages(history, user_message_count)

    response = call_openrouter(model, messages)

    if response.status_code == 200:
        result = response.json()
        generated_text = result["choices"][0]["message"]["content"]
        return jsonify({"response": generated_text, "done": True, "model": model})
    else:
        return (
            jsonify({"error": f"OpenRouter API error: {response.text}"}),
            response.status_code,
        )


@app.route("/api/generate", methods=["POST"])
def generate():
    """
    Legacy Ollama-compatible endpoint — kept for backward compatibility.
    Forwards to OpenRouter using the same model logic.
    """
    data = request.json
    model = data.get("model", "z-ai/glm-4.5-air:free")
    user_message_count = data.get("userMessageCount", 1)

    # Accept full conversation history; fall back to single prompt for old clients
    history = data.get("messages")
    if not history:
        user_prompt = data.get("prompt", "")
        history = [{"role": "user", "content": user_prompt}]

    messages = build_messages(history, user_message_count)

    response = call_openrouter(model, messages)

    if response.status_code == 200:
        result = response.json()
        generated_text = result["choices"][0]["message"]["content"]
        return jsonify({"response": generated_text, "done": True, "model": model})
    else:
        return (
            jsonify({"error": f"OpenRouter API error: {response.text}"}),
            response.status_code,
        )


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
