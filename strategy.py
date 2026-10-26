"""
RicchStrategy — Logique de trading (EMA Pullback)
Tourne en continu, génère des signaux, envoie sur Telegram.
"""
import numpy as np
import pandas as pd
import requests
import os
import time
import json
from datetime import datetime, timezone

TELEGRAM_TOKEN  = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

PAIRS = {
    "BTC/USDT": {"symbol": "BTCUSDT", "emoji": "₿"},
    "ETH/USDT": {"symbol": "ETHUSDT", "emoji": "Ξ"},
    "SOL/USDT": {"symbol": "SOLUSDT", "emoji": "◎"},
    "BNB/USDT": {"symbol": "BNBUSDT", "emoji": "🔶"},
    "ADA/USDT": {"symbol": "ADAUSDT", "emoji": "₳"},
}

# État global des signaux (partagé avec Flask)
signals_state = {pair: {
    "signal": "NEUTRE",
    "price": 0,
    "sl": 0,
    "tp": 0,
    "change_24h": 0,
    "last_update": None,
    "in_trade": False,
    "entry_price": 0,
    "pnl_pct": 0,
} for pair in PAIRS}

trade_history = []


def get_binance_klines(symbol: str, interval="1d", limit=100) -> pd.DataFrame | None:
    """Récupère les données de prix depuis Binance (API publique, sans clé)."""
    url = f"https://api.binance.com/api/v3/klines"
    params = {"symbol": symbol, "interval": interval, "limit": limit}
    try:
        r = requests.get(url, params=params, timeout=10)
        r.raise_for_status()
        data = r.json()
        df = pd.DataFrame(data, columns=[
            "open_time","open","high","low","close","volume",
            "close_time","qav","trades","tbbav","tbqav","ignore"
        ])
        for col in ["open","high","low","close","volume"]:
            df[col] = df[col].astype(float)
        df["open_time"] = pd.to_datetime(df["open_time"], unit="ms")
        df = df.set_index("open_time")
        return df
    except Exception as e:
        print(f"Binance error {symbol}: {e}")
        return None


def compute_signal(df: pd.DataFrame) -> dict:
    """Calcule le signal EMA Pullback pour une paire."""
    if df is None or len(df) < 55:
        return {"signal": "DONNÉES MANQUANTES", "sl": 0, "tp": 0}

    close = df["close"].values
    high  = df["high"].values
    low   = df["low"].values

    # EMA
    ema20 = pd.Series(close).ewm(span=20, adjust=False).mean().values
    ema50 = pd.Series(close).ewm(span=50, adjust=False).mean().values

    # ATR
    h, l, c = pd.Series(high), pd.Series(low), pd.Series(close)
    tr  = pd.concat([h-l, (h-c.shift()).abs(), (l-c.shift()).abs()], axis=1).max(axis=1)
    atr = tr.ewm(span=14, adjust=False).mean().values

    # RSI
    s = pd.Series(close).diff()
    g = s.clip(lower=0).ewm(alpha=1/14, adjust=False).mean()
    lo = (-s.clip(upper=0)).ewm(alpha=1/14, adjust=False).mean()
    rsi = (100 - 100/(1 + g/lo.replace(0, 1e-10))).values

    price   = close[-1]
    e20     = ema20[-1]; e20_3 = ema20[-3]
    e50     = ema50[-1]; e50_3 = ema50[-3]
    atr_val = atr[-1]
    rsi_val = rsi[-1]
    low_cur = low[-1]

    # Conditions
    uptrend  = e20 > e50 and e20 > e20_3 and e50 > e50_3
    pullback = low_cur <= e20 * 1.005 and price > e20 * 0.97
    rsi_ok   = rsi_val > 42

    sl = min(e20 - 2.5 * atr_val, price * 0.96)
    tp = price + 4.0 * atr_val
    risk = price - sl
    if (tp - price) / max(risk, 1e-10) < 1.3:
        tp = price + 1.5 * risk

    change_24h = (close[-1] / close[-2] - 1) * 100 if len(close) > 1 else 0

    if uptrend and pullback and rsi_ok:
        return {
            "signal":     "ACHETER 🟢",
            "sl":         round(sl, 4),
            "tp":         round(tp, 4),
            "sl_pct":     round((price - sl) / price * 100, 2),
            "tp_pct":     round((tp - price) / price * 100, 2),
            "rsi":        round(rsi_val, 1),
            "ema20":      round(e20, 4),
            "change_24h": round(change_24h, 2),
        }
    elif not uptrend or price < e50 * 0.99:
        return {
            "signal":     "VENDRE 🔴",
            "sl":         0, "tp": 0, "sl_pct": 0, "tp_pct": 0,
            "rsi":        round(rsi_val, 1),
            "ema20":      round(e20, 4),
            "change_24h": round(change_24h, 2),
        }
    else:
        return {
            "signal":     "NEUTRE ⚪",
            "sl":         0, "tp": 0, "sl_pct": 0, "tp_pct": 0,
            "rsi":        round(rsi_val, 1),
            "ema20":      round(e20, 4),
            "change_24h": round(change_24h, 2),
        }


def send_telegram(message: str):
    """Envoie un message sur Telegram."""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print(f"[Telegram désactivé] {message}")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        requests.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "HTML"
        }, timeout=10)
    except Exception as e:
        print(f"Telegram error: {e}")


def format_signal_message(pair: str, price: float, sig: dict) -> str:
    emoji = PAIRS[pair]["emoji"]
    return (
        f"{emoji} <b>{pair}</b>\n"
        f"━━━━━━━━━━━━━━\n"
        f"Signal : <b>{sig['signal']}</b>\n"
        f"Prix   : <b>{price:,.2f} $</b>\n"
        f"RSI    : {sig.get('rsi', 'N/A')}\n"
        + (f"🛑 Stop Loss  : {sig['sl']:,.2f} $ (-{sig['sl_pct']}%)\n"
           f"🎯 Take Profit: {sig['tp']:,.2f} $ (+{sig['tp_pct']}%)\n"
           if sig.get('sl') else "") +
        f"📅 {datetime.now(timezone.utc).strftime('%d/%m %H:%M')} UTC"
    )


def run_signals_loop():
    """Boucle principale — tourne en arrière-plan toutes les heures."""
    print("🤖 Bot de signaux démarré")
    previous_signals = {pair: None for pair in PAIRS}

    while True:
        for pair, info in PAIRS.items():
            df = get_binance_klines(info["symbol"])
            price = df["close"].iloc[-1] if df is not None else 0
            sig   = compute_signal(df)

            signals_state[pair].update({
                "signal":      sig.get("signal", "ERREUR"),
                "price":       round(price, 4),
                "sl":          sig.get("sl", 0),
                "tp":          sig.get("tp", 0),
                "sl_pct":      sig.get("sl_pct", 0),
                "tp_pct":      sig.get("tp_pct", 0),
                "rsi":         sig.get("rsi", 0),
                "ema20":       sig.get("ema20", 0),
                "change_24h":  sig.get("change_24h", 0),
                "last_update": datetime.now(timezone.utc).isoformat(),
            })

            # Envoie Telegram seulement si le signal change
            current_sig = sig.get("signal", "")
            if "ACHETER" in current_sig and previous_signals[pair] != current_sig:
                msg = format_signal_message(pair, price, sig)
                send_telegram(msg)
                trade_history.append({
                    "pair":   pair,
                    "action": "ACHAT",
                    "price":  price,
                    "sl":     sig.get("sl", 0),
                    "tp":     sig.get("tp", 0),
                    "time":   datetime.now(timezone.utc).isoformat(),
                })

            previous_signals[pair] = current_sig
            time.sleep(1)

        print(f"[{datetime.now().strftime('%H:%M:%S')}] Signaux mis à jour")
        time.sleep(3600)  # Attendre 1 heure
