"""
RicchTrading Dashboard — Serveur Flask
"""
import threading
from flask import Flask, render_template, jsonify
from strategy import signals_state, trade_history, run_signals_loop

app = Flask(__name__)

# Démarre le bot au chargement du module (compatible Gunicorn)
_bot_started = False
def _start_bot():
    global _bot_started
    if not _bot_started:
        _bot_started = True
        t = threading.Thread(target=run_signals_loop, daemon=True)
        t.start()
        print("🤖 Thread bot démarré")

_start_bot()

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/signals")
def api_signals():
    return jsonify(signals_state)

@app.route("/api/history")
def api_history():
    return jsonify(trade_history[-50:])

@app.route("/api/health")
def health():
    return jsonify({"status": "ok", "bot_started": _bot_started})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
