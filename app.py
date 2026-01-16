import os
from flask import Flask, request, jsonify

app = Flask(__name__)

@app.get("/")
def home():
    return "AI Receptionist is running", 200

@app.post("/webhook")
def webhook():
    data = request.get_json(silent=True) or {}
    return jsonify({"received": data}), 200

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
