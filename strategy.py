"""
RicchStrategy — Logique de trading (EMA Pullback)
Tourne en continu, génère des signaux, envoie sur Telegram.
"""
import numpy as np
import pandas as pd
import requests
import os
import json
import time
from datetime import datetime, timezone, timedelta

TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# Fichier de persistance (survit aux redémarrages Render si /tmp non purgé,
# ou mieux : monter un Render Disk sur /data)
PERSIST_FILE = os.environ.get("PERSIST_FILE", "/tmp/ricch_trades.json")

PAIRS = {
    "BTC/USDT": {"symbol": "BTCUSDT", "coingecko": "bitcoin",     "emoji": "₿",  "ref_price": 67000, "vol": 0.028},
    "ETH/USDT": {"symbol": "ETHUSDT", "coingecko": "ethereum",    "emoji": "Ξ",  "ref_price":  3500, "vol": 0.033},
    "SOL/USDT": {"symbol": "SOLUSDT", "coingecko": "solana",      "emoji": "◎",  "ref_price":   175, "vol": 0.042},
    "BNB/USDT": {"symbol": "BNBUSDT", "coingecko": "binancecoin", "emoji": "🔶", "ref_price":   580, "vol": 0.026},
    "ADA/USDT": {"symbol": "ADAUSDT", "coingecko": "cardano",     "emoji": "₳",  "ref_price":  0.45, "vol": 0.040},
}

# État global des signaux (partagé avec Flask)
signals_state = {pair: {
    "signal": "NEUTRE", "price": 0, "sl": 0, "tp": 0,
    "change_24h": 0, "last_update": None, "rsi": 0,
    "source": "—",
} for pair in PAIRS}

trade_history = []   # liste de trades avec statut (open/tp/sl)
_open_trades  = {}   # pair → trade en cours pour suivi résolution


# ── Persistance JSON ─────────────────────────────────────────────────────────

def _load_persist():
    """Charge trade_history et _open_trades depuis le fichier JSON au démarrage."""
    global trade_history, _open_trades
    try:
        if os.path.exists(PERSIST_FILE):
            with open(PERSIST_FILE, "r") as f:
                data = json.load(f)
            trade_history = data.get("trade_history", [])
            _open_trades  = data.get("open_trades", {})
            print(f"✅ Persistance chargée : {len(trade_history)} trades, {len(_open_trades)} ouverts")
    except Exception as e:
        print(f"⚠ Erreur chargement persistance : {e}")

def _save_persist():
    """Sauvegarde trade_history et _open_trades dans le fichier JSON."""
    try:
        with open(PERSIST_FILE, "w") as f:
            json.dump({"trade_history": trade_history, "open_trades": _open_trades}, f)
    except Exception as e:
        print(f"⚠ Erreur sauvegarde persistance : {e}")

# Chargement au démarrage du module
_load_persist()


# ── Sources de données ──────────────────────────────────────────────────────

def get_klines(pair_info: dict, limit=100):
    """Essaie Binance → CoinGecko → données synthétiques. Retourne (df, source)."""
    df = _binance_klines(pair_info["symbol"], limit)
    if df is not None:
        return df, "Binance"

    df = _coingecko_ohlc(pair_info["coingecko"], limit)
    if df is not None:
        return df, "CoinGecko"

    print(f"[{pair_info['symbol']}] Toutes les APIs inaccessibles — données synthétiques")
    return _synthetic_ohlc(pair_info["ref_price"], pair_info["vol"], limit), "Synthétique"


def _binance_klines(symbol: str, limit=100) -> pd.DataFrame | None:
    for base_url in ["https://api.binance.com/api/v3/klines", "https://api.binance.us/api/v3/klines"]:
        try:
            r = requests.get(base_url, params={"symbol": symbol, "interval": "1d", "limit": limit}, timeout=8)
            r.raise_for_status()
            data = r.json()
            if not data:
                continue
            df = pd.DataFrame(data, columns=[
                "open_time","open","high","low","close","volume",
                "close_time","qav","trades","tbbav","tbqav","ignore"
            ])
            for col in ["open","high","low","close","volume"]:
                df[col] = df[col].astype(float)
            df["open_time"] = pd.to_datetime(df["open_time"], unit="ms")
            return df.set_index("open_time")
        except Exception as e:
            print(f"Binance error {symbol} ({base_url}): {e}")
    return None


def _coingecko_ohlc(coin_id: str, limit=100) -> pd.DataFrame | None:
    url = f"https://api.coingecko.com/api/v3/coins/{coin_id}/ohlc"
    try:
        r = requests.get(url, params={"vs_currency": "usd", "days": min(limit, 90)}, timeout=15)
        r.raise_for_status()
        data = r.json()
        if not data:
            return None
        df = pd.DataFrame(data, columns=["open_time","open","high","low","close"])
        df["open_time"] = pd.to_datetime(df["open_time"], unit="ms")
        df = df.set_index("open_time")
        df["volume"] = 0.0
        return df
    except Exception as e:
        print(f"CoinGecko error {coin_id}: {e}")
        return None


def _synthetic_ohlc(ref_price: float, daily_vol: float, n: int = 100) -> pd.DataFrame:
    """GBM réaliste avec tendance haussière légère (pour démo)."""
    rng = np.random.default_rng(42)
    mu = 0.0003  # drift légèrement positif
    returns = rng.normal(mu, daily_vol, n)
    closes = ref_price * np.exp(np.cumsum(returns))
    highs  = closes * (1 + abs(rng.normal(0, daily_vol * 0.5, n)))
    lows   = closes * (1 - abs(rng.normal(0, daily_vol * 0.5, n)))
    opens  = np.roll(closes, 1); opens[0] = ref_price

    dates = [datetime.now(timezone.utc) - timedelta(days=n - i) for i in range(n)]
    return pd.DataFrame({
        "open": opens, "high": highs, "low": lows, "close": closes, "volume": 0.0
    }, index=pd.DatetimeIndex(dates))


# ── Stratégie ───────────────────────────────────────────────────────────────

def compute_signal(df: pd.DataFrame) -> dict:
    if df is None or len(df) < 55:
        return {"signal": "DONNÉES MANQUANTES", "sl": 0, "tp": 0}

    close = df["close"].values
    high  = df["high"].values
    low   = df["low"].values

    ema20 = pd.Series(close).ewm(span=20, adjust=False).mean().values
    ema50 = pd.Series(close).ewm(span=50, adjust=False).mean().values

    h, l, c = pd.Series(high), pd.Series(low), pd.Series(close)
    tr  = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    atr = tr.ewm(span=14, adjust=False).mean().values

    s  = pd.Series(close).diff()
    g  = s.clip(lower=0).ewm(alpha=1/14, adjust=False).mean()
    lo = (-s.clip(upper=0)).ewm(alpha=1/14, adjust=False).mean()
    rsi = (100 - 100 / (1 + g / lo.replace(0, 1e-10))).values

    price   = close[-1]
    e20, e20_3 = ema20[-1], ema20[-3]
    e50, e50_3 = ema50[-1], ema50[-3]
    atr_val    = atr[-1]
    rsi_val    = rsi[-1]
    low_cur    = low[-1]

    uptrend  = e20 > e50 and e20 > e20_3 and e50 > e50_3
    pullback = low_cur <= e20 * 1.005 and price > e20 * 0.97
    rsi_ok   = rsi_val > 42

    sl   = min(e20 - 2.5 * atr_val, price * 0.96)
    tp   = price + 4.0 * atr_val
    risk = price - sl
    if (tp - price) / max(risk, 1e-10) < 1.3:
        tp = price + 1.5 * risk

    change_24h = (close[-1] / close[-2] - 1) * 100 if len(close) > 1 else 0

    base = {
        "rsi": round(rsi_val, 1),
        "ema20": round(e20, 4),
        "change_24h": round(change_24h, 2),
        "sl": 0, "tp": 0, "sl_pct": 0, "tp_pct": 0,
    }

    if uptrend and pullback and rsi_ok:
        return {**base,
            "signal":  "ACHETER 🟢",
            "sl":      round(sl, 4),
            "tp":      round(tp, 4),
            "sl_pct":  round((price - sl) / price * 100, 2),
            "tp_pct":  round((tp - price) / price * 100, 2),
        }
    elif not uptrend or price < e50 * 0.99:
        return {**base, "signal": "VENDRE 🔴"}
    else:
        return {**base, "signal": "NEUTRE ⚪"}


# ── Telegram ────────────────────────────────────────────────────────────────

def send_telegram(message: str):
    if not TELEGRAM_TOKEN or TELEGRAM_TOKEN == "placeholder":
        print(f"[Telegram désactivé] {message[:80]}")
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"},
            timeout=10
        )
    except Exception as e:
        print(f"Telegram error: {e}")


def format_signal_message(pair: str, price: float, sig: dict) -> str:
    emoji = PAIRS[pair]["emoji"]
    sl_tp = (f"🛑 Stop Loss  : {sig['sl']:,.4f} $ (-{sig['sl_pct']}%)\n"
             f"🎯 Take Profit: {sig['tp']:,.4f} $ (+{sig['tp_pct']}%)\n"
             if sig.get("sl") else "")
    return (
        f"{emoji} <b>{pair}</b>\n━━━━━━━━━━━━━━\n"
        f"Signal : <b>{sig['signal']}</b>\n"
        f"Prix   : <b>{price:,.4f} $</b>\n"
        f"RSI    : {sig.get('rsi', 'N/A')}\n"
        f"{sl_tp}"
        f"📅 {datetime.now(timezone.utc).strftime('%d/%m %H:%M')} UTC"
    )


# ── Boucle principale ────────────────────────────────────────────────────────

def run_signals_loop():
    print("🤖 Bot de signaux démarré")
    previous_signals = {pair: None for pair in PAIRS}

    while True:
        print(f"[{datetime.now().strftime('%H:%M:%S')}] Cycle de mise à jour...")
        for pair, info in PAIRS.items():
            try:
                df, source = get_klines(info)
                price = float(df["close"].iloc[-1]) if df is not None else 0
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
                    "source":      source,
                })
                print(f"  {pair}: {sig.get('signal')} @ {price:.4f} [{source}]")

                current_sig = sig.get("signal", "")

                # ── Résolution des trades ouverts ──
                if pair in _open_trades:
                    t = _open_trades[pair]
                    if price >= t["tp"] and t["tp"] > 0:
                        t["status"]    = "TP ✅"
                        t["exit_price"]= round(price, 4)
                        t["exit_time"] = datetime.now(timezone.utc).isoformat()
                        t["pnl_pct"]   = round((price - t["entry"]) / t["entry"] * 100, 2)
                        del _open_trades[pair]
                        _save_persist()
                        print(f"  ✅ {pair} TP touché @ {price:.4f}")
                    elif price <= t["sl"] and t["sl"] > 0:
                        t["status"]    = "SL ❌"
                        t["exit_price"]= round(price, 4)
                        t["exit_time"] = datetime.now(timezone.utc).isoformat()
                        t["pnl_pct"]   = round((price - t["entry"]) / t["entry"] * 100, 2)
                        del _open_trades[pair]
                        _save_persist()
                        print(f"  ❌ {pair} SL touché @ {price:.4f}")

                # ── Nouveau signal achat ──
                if "ACHETER" in current_sig and previous_signals[pair] != current_sig:
                    msg = format_signal_message(pair, price, sig)
                    send_telegram(msg)
                    trade = {
                        "id":         len(trade_history) + 1,
                        "pair":       pair,
                        "action":     "ACHAT",
                        "entry":      round(price, 4),
                        "sl":         sig.get("sl", 0),
                        "tp":         sig.get("tp", 0),
                        "sl_pct":     sig.get("sl_pct", 0),
                        "tp_pct":     sig.get("tp_pct", 0),
                        "rsi":        sig.get("rsi", 0),
                        "time":       datetime.now(timezone.utc).isoformat(),
                        "status":     "Ouvert 🔵",
                        "exit_price": None,
                        "exit_time":  None,
                        "pnl_pct":    None,
                    }
                    trade_history.append(trade)
                    _open_trades[pair] = trade
                    _save_persist()

                previous_signals[pair] = current_sig

            except Exception as e:
                print(f"  ❌ Erreur {pair}: {e}")

            time.sleep(2)

        print(f"[{datetime.now().strftime('%H:%M:%S')}] Cycle terminé — prochain dans 5 min")
        time.sleep(300)
