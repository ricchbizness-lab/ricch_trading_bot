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
import uuid
from datetime import datetime, timezone, timedelta
try:
    import psycopg2
    import psycopg2.extras
except ImportError:
    psycopg2 = None

TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# Fichier de persistance (survit aux redémarrages Render si /tmp non purgé,
# ou mieux : monter un Render Disk sur /data)
DATABASE_URL = os.environ.get("DATABASE_URL", "")

PAIRS = {
    "BTC/USDT":  {"symbol": "BTCUSDT",  "coingecko": "bitcoin",          "emoji": "₿",  "ref_price": 67000, "vol": 0.028},
    "ETH/USDT":  {"symbol": "ETHUSDT",  "coingecko": "ethereum",         "emoji": "Ξ",  "ref_price":  3500, "vol": 0.033},
    "SOL/USDT":  {"symbol": "SOLUSDT",  "coingecko": "solana",           "emoji": "◎",  "ref_price":   175, "vol": 0.042},
    "BNB/USDT":  {"symbol": "BNBUSDT",  "coingecko": "binancecoin",      "emoji": "🔶", "ref_price":   580, "vol": 0.026},
    "ADA/USDT":  {"symbol": "ADAUSDT",  "coingecko": "cardano",          "emoji": "₳",  "ref_price":  0.45, "vol": 0.040},
    "XRP/USDT":  {"symbol": "XRPUSDT",  "coingecko": "ripple",           "emoji": "💧", "ref_price":  0.55, "vol": 0.038},
    "DOGE/USDT": {"symbol": "DOGEUSDT", "coingecko": "dogecoin",         "emoji": "🐶", "ref_price":  0.12, "vol": 0.052},
    "AVAX/USDT": {"symbol": "AVAXUSDT", "coingecko": "avalanche-2",      "emoji": "🔺", "ref_price":    38, "vol": 0.048},
    "DOT/USDT":  {"symbol": "DOTUSDT",  "coingecko": "polkadot",         "emoji": "●",  "ref_price":     8, "vol": 0.045},
    "LINK/USDT": {"symbol": "LINKUSDT", "coingecko": "chainlink",        "emoji": "⬡",  "ref_price":    15, "vol": 0.044},
    "MATIC/USDT":{"symbol": "MATICUSDT","coingecko": "matic-network",    "emoji": "🟣", "ref_price":  0.80, "vol": 0.050},
    "LTC/USDT":  {"symbol": "LTCUSDT",  "coingecko": "litecoin",         "emoji": "Ł",  "ref_price":    85, "vol": 0.035},
    "UNI/USDT":  {"symbol": "UNIUSDT",  "coingecko": "uniswap",          "emoji": "🦄", "ref_price":     9, "vol": 0.046},
    "ATOM/USDT": {"symbol": "ATOMUSDT", "coingecko": "cosmos",           "emoji": "⚛",  "ref_price":    10, "vol": 0.047},
    "NEAR/USDT": {"symbol": "NEARUSDT", "coingecko": "near",             "emoji": "🌐", "ref_price":     5, "vol": 0.051},
}

# État global des signaux (partagé avec Flask)
signals_state = {pair: {
    "signal": "NEUTRE", "price": 0, "sl": 0, "tp": 0,
    "sell_target": 0, "change_24h": 0, "last_update": None, "rsi": 0,
    "source": "—",
} for pair in PAIRS}

trade_history = []   # liste de trades avec statut (open/tp/sl)
_open_trades  = {}   # pair → trade en cours pour suivi résolution


# ── Persistance Neon (PostgreSQL) ─────────────────────────────────────────────

def _get_conn():
    """Ouvre une connexion Neon depuis DATABASE_URL."""
    if not DATABASE_URL or not psycopg2:
        return None
    try:
        return psycopg2.connect(DATABASE_URL, sslmode="require")
    except Exception as e:
        print(f"⚠ Neon connexion échouée : {e}")
        return None

def _init_db():
    """Crée la table trades si elle n'existe pas, avec migration colonne trade_id."""
    conn = _get_conn()
    if not conn:
        return
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS trades (
                    id          SERIAL PRIMARY KEY,
                    trade_id    TEXT UNIQUE,
                    data        JSONB NOT NULL,
                    is_open     BOOLEAN DEFAULT FALSE,
                    pair        TEXT,
                    created_at  TIMESTAMPTZ DEFAULT NOW()
                )
            """)
            # Migration douce : ajoute trade_id si absent (table existante)
            cur.execute("""
                ALTER TABLE trades ADD COLUMN IF NOT EXISTS trade_id TEXT UNIQUE
            """)
            # Backfill des anciens rows sans trade_id
            cur.execute("""
                UPDATE trades SET trade_id = id::TEXT
                WHERE trade_id IS NULL
            """)
        conn.commit()
        print("✅ Table Neon initialisée / migrée")
    except Exception as e:
        print(f"⚠ Neon init table : {e}")
    finally:
        conn.close()

def _load_persist():
    """Charge trade_history et _open_trades depuis Neon au démarrage."""
    global trade_history, _open_trades
    conn = _get_conn()
    if not conn:
        print("⚠ Pas de DATABASE_URL — persistance désactivée")
        return
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT data, is_open, pair FROM trades ORDER BY id ASC")
            rows = cur.fetchall()
        trade_history = [dict(r["data"]) for r in rows]
        _open_trades  = {r["pair"]: dict(r["data"]) for r in rows if r["is_open"]}
        print(f"✅ Neon chargé : {len(trade_history)} trades, {len(_open_trades)} ouverts")
    except Exception as e:
        print(f"⚠ Erreur chargement Neon : {e}")
    finally:
        conn.close()

def _save_trade(trade: dict, is_open: bool = False):
    """Insère un trade dans Neon (upsert par trade_id UUID)."""
    conn = _get_conn()
    if not conn:
        return
    try:
        trade_id = str(trade.get("id", ""))
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO trades (trade_id, data, is_open, pair)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (trade_id) DO UPDATE
                    SET data = EXCLUDED.data,
                        is_open = EXCLUDED.is_open
            """, (trade_id, json.dumps(trade), is_open, trade.get("pair")))
        conn.commit()
        print(f"  💾 Trade sauvegardé Neon : {trade.get('pair')} id={trade_id[:8]}")
    except Exception as e:
        print(f"⚠ Erreur save trade Neon : {e}")
    finally:
        conn.close()

def _update_trade(trade: dict):
    """Clôture un trade (TP/SL touché) en cherchant par trade_id UUID."""
    conn = _get_conn()
    if not conn:
        return
    try:
        trade_id = str(trade.get("id", ""))
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE trades SET data = %s, is_open = FALSE
                WHERE trade_id = %s
            """, (json.dumps(trade), trade_id))
            updated = cur.rowcount
        conn.commit()
        if updated:
            print(f"  💾 Trade clôturé Neon : {trade.get('pair')} id={trade_id[:8]} → {trade.get('status')}")
        else:
            print(f"  ⚠ Trade non trouvé pour update : {trade.get('pair')} id={trade_id[:8]}")
    except Exception as e:
        print(f"⚠ Erreur update trade Neon : {e}")
    finally:
        conn.close()

def _save_persist():
    """Alias de compatibilité — ne fait rien, les saves sont maintenant atomiques."""
    pass

# Initialisation et chargement au démarrage du module
_init_db()
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
            r = requests.get(base_url, params={"symbol": symbol, "interval": "1h", "limit": limit}, timeout=8)
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

# Capital de référence pour le sizing (modifiable via env var)
CAPITAL = float(os.environ.get("CAPITAL", "100"))  # € de capital total supposé
RISK_PCT = 0.02  # 2% du capital risqué par trade

def _position_size(price: float, sl: float) -> dict:
    """
    Calcule la taille de position optimale selon le risque fixé.
    Formule : mise = (capital × risk_pct) / (prix - sl)
    Retourne le montant € à investir et la quantité de coins.
    """
    risk_per_unit = price - sl
    if risk_per_unit <= 0:
        return {"amount_eur": 0, "qty": 0, "risk_eur": 0}
    risk_eur   = round(CAPITAL * RISK_PCT, 2)
    qty        = risk_eur / risk_per_unit
    amount_eur = round(qty * price, 2)
    return {
        "amount_eur": amount_eur,
        "qty":        round(qty, 6),
        "risk_eur":   risk_eur,
    }

def compute_signal(df: pd.DataFrame) -> dict:
    if df is None or len(df) < 55:
        return {"signal": "DONNÉES MANQUANTES", "sl": 0, "tp": 0}

    close  = df["close"].values
    high   = df["high"].values
    low    = df["low"].values
    volume = df["volume"].values if "volume" in df.columns else np.ones(len(close))

    # ── Indicateurs communs ──
    ema12 = pd.Series(close).ewm(span=12, adjust=False).mean().values
    ema20 = pd.Series(close).ewm(span=20, adjust=False).mean().values
    ema26 = pd.Series(close).ewm(span=26, adjust=False).mean().values
    ema50 = pd.Series(close).ewm(span=50, adjust=False).mean().values

    h, l, c = pd.Series(high), pd.Series(low), pd.Series(close)
    tr  = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    atr = tr.ewm(span=14, adjust=False).mean().values

    s  = pd.Series(close).diff()
    g  = s.clip(lower=0).ewm(alpha=1/14, adjust=False).mean()
    lo = (-s.clip(upper=0)).ewm(alpha=1/14, adjust=False).mean()
    rsi = (100 - 100 / (1 + g / lo.replace(0, 1e-10))).values

    price      = close[-1]
    e12, e12_1 = ema12[-1], ema12[-2]
    e20, e20_3 = ema20[-1], ema20[-3]
    e26, e26_1 = ema26[-1], ema26[-2]
    e50, e50_3 = ema50[-1], ema50[-3]
    atr_val    = atr[-1]
    rsi_val    = rsi[-1]
    low_cur    = low[-1]

    change_24h = (close[-1] / close[-2] - 1) * 100 if len(close) > 1 else 0

    base = {
        "rsi":        round(rsi_val, 1),
        "ema20":      round(e20, 4),
        "change_24h": round(change_24h, 2),
        "sl": 0, "tp": 0, "sl_pct": 0, "tp_pct": 0,
        "strategy":   "",
    }

    # ── Stratégie 1 : EMA Pullback ──────────────────────────────────────────
    # Tendance haussière (EMA20 > EMA50, les deux montent)
    # + prix revient toucher EMA20 (pullback)
    # + RSI > 38 (momentum positif, pas survendu extrême)
    uptrend  = e20 > e50 and e20 > e20_3 and e50 > e50_3
    pullback = low_cur <= e20 * 1.01 and price > e20 * 0.96
    rsi_ok   = rsi_val > 38

    if uptrend and pullback and rsi_ok:
        sl   = min(e20 - 2.5 * atr_val, price * 0.96)
        tp   = price + 4.0 * atr_val
        risk = price - sl
        if (tp - price) / max(risk, 1e-10) < 1.3:
            tp = price + 1.5 * risk
        sizing = _position_size(price, sl)
        return {**base,
            "signal":     "ACHETER 🟢",
            "strategy":   "EMA Pullback",
            "sl":         round(sl, 4),
            "tp":         round(tp, 4),
            "sl_pct":     round((price - sl) / price * 100, 2),
            "tp_pct":     round((tp - price) / price * 100, 2),
            "amount_eur": sizing["amount_eur"],
            "qty":        sizing["qty"],
            "risk_eur":   sizing["risk_eur"],
        }

    # ── Stratégie 2 : EMA Crossover ─────────────────────────────────────────
    # EMA12 vient de croiser au-dessus EMA26 (momentum haussier court terme)
    # + prix au-dessus EMA50 (tendance de fond positive)
    # + RSI entre 45-65 (ni suracheté ni survendu — momentum sain)
    crossover    = e12 > e26 and e12_1 <= e26_1   # croisement haussier ce cycle
    above_trend  = price > e50
    rsi_momentum = 45 <= rsi_val <= 65

    if crossover and above_trend and rsi_momentum:
        sl   = min(e26 - 1.5 * atr_val, price * 0.97)
        tp   = price + 3.0 * atr_val
        risk = price - sl
        if (tp - price) / max(risk, 1e-10) < 1.5:
            tp = price + 2.0 * risk
        sizing = _position_size(price, sl)
        return {**base,
            "signal":     "ACHETER 🟢",
            "strategy":   "EMA Crossover",
            "sl":         round(sl, 4),
            "tp":         round(tp, 4),
            "sl_pct":     round((price - sl) / price * 100, 2),
            "tp_pct":     round((tp - price) / price * 100, 2),
            "amount_eur": sizing["amount_eur"],
            "qty":        sizing["qty"],
            "risk_eur":   sizing["risk_eur"],
        }

    # ── Signal ALLÉGER : surachat RSI > 70 + prix > EMA20 × 1.02 ───────────
    if rsi_val > 70 and price > e20 * 1.02:
        return {**base,
            "signal":      "ALLÉGER 🟠",
            "sell_target": round(price, 4),
        }

    # ── Signal BAISSIER : tendance renversée ────────────────────────────────
    if not uptrend or price < e50 * 0.99:
        return {**base, "signal": "BAISSIER 🔴"}

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
    signal = sig.get("signal", "")

    if "ALLÉGER" in signal:
        return (
            f"{emoji} <b>{pair}</b>\n━━━━━━━━━━━━━━\n"
            f"Signal : <b>{signal}</b>\n"
            f"Prix actuel : <b>{price:,.4f} $</b>\n"
            f"💰 Prix de vente conseillé : <b>{sig.get('sell_target', price):,.4f} $</b>\n"
            f"RSI : {sig.get('rsi', 'N/A')} ⚠️ Zone de surachat\n"
            f"📌 Si vous détenez cette crypto, c'est une zone de prise de profit.\n"
            f"📅 {datetime.now(timezone.utc).strftime('%d/%m %H:%M')} UTC"
        )

    # Bloc SL/TP
    sl_tp = ""
    if sig.get("sl"):
        sl_tp = (
            f"🛑 Stop Loss  : {sig['sl']:,.4f} $ (-{sig['sl_pct']}%)\n"
            f"🎯 Take Profit: {sig['tp']:,.4f} $ (+{sig['tp_pct']}%)\n"
        )

    # Bloc position sizing (uniquement pour les signaux ACHETER)
    sizing_line = ""
    if "ACHETER" in signal and sig.get("amount_eur") and sig["amount_eur"] > 0:
        strategy = sig.get("strategy", "")
        strat_tag = f" [{strategy}]" if strategy else ""
        sizing_line = (
            f"💼 Position conseillée{strat_tag} :\n"
            f"   Mise : <b>{sig['amount_eur']} €</b> sur {CAPITAL} € de capital\n"
            f"   Risque max : {sig['risk_eur']} € (2% du capital)\n"
        )

    return (
        f"{emoji} <b>{pair}</b>\n━━━━━━━━━━━━━━\n"
        f"Signal : <b>{signal}</b>\n"
        f"Prix   : <b>{price:,.4f} $</b>\n"
        f"RSI    : {sig.get('rsi', 'N/A')}\n"
        f"{sl_tp}"
        f"{sizing_line}"
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
                    "sell_target": sig.get("sell_target", 0),
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
                        _update_trade(t)
                        print(f"  ✅ {pair} TP touché @ {price:.4f}")
                    elif price <= t["sl"] and t["sl"] > 0:
                        t["status"]    = "SL ❌"
                        t["exit_price"]= round(price, 4)
                        t["exit_time"] = datetime.now(timezone.utc).isoformat()
                        t["pnl_pct"]   = round((price - t["entry"]) / t["entry"] * 100, 2)
                        del _open_trades[pair]
                        _update_trade(t)
                        print(f"  ❌ {pair} SL touché @ {price:.4f}")

                # ── Nouveau signal achat ──
                if "ACHETER" in current_sig and previous_signals[pair] != current_sig:
                    msg = format_signal_message(pair, price, sig)
                    send_telegram(msg)
                    trade = {
                        "id":         str(uuid.uuid4()),
                        "pair":       pair,
                        "action":     "ACHAT",
                        "strategy":   sig.get("strategy", "EMA Pullback"),
                        "entry":      round(price, 4),
                        "sl":         sig.get("sl", 0),
                        "tp":         sig.get("tp", 0),
                        "sl_pct":     sig.get("sl_pct", 0),
                        "tp_pct":     sig.get("tp_pct", 0),
                        "rsi":        sig.get("rsi", 0),
                        "amount_eur": sig.get("amount_eur", 0),
                        "risk_eur":   sig.get("risk_eur", 0),
                        "time":       datetime.now(timezone.utc).isoformat(),
                        "status":     "Ouvert 🔵",
                        "exit_price": None,
                        "exit_time":  None,
                        "pnl_pct":    None,
                    }
                    trade_history.append(trade)
                    _open_trades[pair] = trade
                    _save_trade(trade, is_open=True)

                # ── Nouveau signal alléger ──
                elif "ALLÉGER" in current_sig and previous_signals[pair] != current_sig:
                    msg = format_signal_message(pair, price, sig)
                    send_telegram(msg)
                    trade = {
                        "id":          str(uuid.uuid4()),
                        "pair":        pair,
                        "action":      "VENTE",
                        "entry":       round(price, 4),
                        "sell_target": sig.get("sell_target", round(price, 4)),
                        "sl":          0,
                        "tp":          0,
                        "sl_pct":      0,
                        "tp_pct":      0,
                        "rsi":         sig.get("rsi", 0),
                        "time":        datetime.now(timezone.utc).isoformat(),
                        "status":      "Alerte 🟠",
                        "exit_price":  None,
                        "exit_time":   None,
                        "pnl_pct":     None,
                    }
                    trade_history.append(trade)
                    _save_trade(trade, is_open=False)

                previous_signals[pair] = current_sig

            except Exception as e:
                print(f"  ❌ Erreur {pair}: {e}")

            time.sleep(2)

        print(f"[{datetime.now().strftime('%H:%M:%S')}] Cycle terminé — prochain dans 30 min")
        time.sleep(1800)
