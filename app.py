"""
RicchTrading Dashboard — Serveur Flask
"""
import threading
from flask import Flask, render_template, jsonify
from strategy import signals_state, trade_history, run_signals_loop, _open_trades

app = Flask(__name__)

_bot_thread = None

def _ensure_bot_running():
    """Démarre ou redémarre le bot si le thread est mort (spin-down Render)."""
    global _bot_thread
    if _bot_thread is None or not _bot_thread.is_alive():
        _bot_thread = threading.Thread(target=_safe_loop, daemon=True, name="bot")
        _bot_thread.start()
        print("🤖 Thread bot (re)démarré")

def _safe_loop():
    """Wrapper qui relance la boucle en cas d'exception inattendue."""
    import time
    while True:
        try:
            run_signals_loop()
        except Exception as e:
            print(f"❌ Bot crash: {e} — redémarrage dans 30s")
            time.sleep(30)

# Démarrage au chargement du module (Gunicorn compatible)
_ensure_bot_running()


@app.route("/")
def index():
    _ensure_bot_running()  # relance si spin-down
    return render_template("index.html")

@app.route("/api/signals")
def api_signals():
    _ensure_bot_running()
    return jsonify(signals_state)

@app.route("/api/history")
def api_history():
    return jsonify(trade_history[-50:])

@app.route("/api/performance")
def api_performance():
    closed = [t for t in trade_history if t.get("pnl_pct") is not None]
    wins   = [t for t in closed if t["pnl_pct"] > 0]
    losses = [t for t in closed if t["pnl_pct"] <= 0]
    total_pnl = round(sum(t["pnl_pct"] for t in closed), 2) if closed else 0
    win_rate  = round(len(wins) / len(closed) * 100, 1) if closed else 0
    avg_win   = round(sum(t["pnl_pct"] for t in wins)   / len(wins),   2) if wins   else 0
    avg_loss  = round(sum(t["pnl_pct"] for t in losses) / len(losses), 2) if losses else 0
    # courbe cumulative P&L
    cumulative, running = [], 0
    for t in closed:
        running += t["pnl_pct"]
        cumulative.append({"time": t["exit_time"], "pnl": round(running, 2), "pair": t["pair"]})
    return jsonify({
        "trades":     trade_history[-100:],
        "stats": {
            "total":     len(trade_history),
            "closed":    len(closed),
            "open":      len(_open_trades),
            "wins":      len(wins),
            "losses":    len(losses),
            "win_rate":  win_rate,
            "total_pnl": total_pnl,
            "avg_win":   avg_win,
            "avg_loss":  avg_loss,
        },
        "cumulative": cumulative,
    })

@app.route("/api/health")
def health():
    alive = _bot_thread is not None and _bot_thread.is_alive()
    return jsonify({"status": "ok", "bot_alive": alive})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
