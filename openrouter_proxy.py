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


@app.route("/api/openrouter", methods=["POST"])
def openrouter():
    data = request.json
    user_prompt = data.get("prompt", "")
    model = data.get("model", "z-ai/glm-4.5-air:free")

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt},
    ]

    response = requests.post(
        OPENROUTER_URL,
        headers={
            "Authorization": f"Bearer {OPENROUTER_API_KEY}",
            "Content-Type": "application/json",
        },
        json={"model": model, "messages": messages},
        timeout=60,
    )

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
    user_prompt = data.get("prompt", "")
    model = data.get("model", "z-ai/glm-4.5-air:free")

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt},
    ]

    response = requests.post(
        OPENROUTER_URL,
        headers={
            "Authorization": f"Bearer {OPENROUTER_API_KEY}",
            "Content-Type": "application/json",
        },
        json={"model": model, "messages": messages},
        timeout=60,
    )

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
