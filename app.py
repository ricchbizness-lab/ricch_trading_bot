"""
RicchTrading Dashboard — Serveur Flask
"""
import threading
from flask import Flask, render_template, jsonify
from strategy import signals_state, trade_history, run_signals_loop

app = Flask(__name__)

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/signals")
def api_signals():
    return jsonify(signals_state)

@app.route("/api/history")
def api_history():
    return jsonify(trade_history[-50:])  # 50 derniers trades

@app.route("/api/health")
def health():
    return jsonify({"status": "ok"})

if __name__ == "__main__":
    # Lancer le bot en arrière-plan
    bot_thread = threading.Thread(target=run_signals_loop, daemon=True)
    bot_thread.start()
    app.run(host="0.0.0.0", port=10000)
