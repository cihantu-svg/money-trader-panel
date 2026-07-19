# -*- coding: utf-8 -*-
"""
OB & BREAKER BLOCK CONFLUENCE SCANNER BOT
- LuxAlgo Order Blocks + Breaker Blocks mantigi
- OB ve Breaker'larin ust uste geldigi (confluence) alanlari tespit eder
- Min %3 mum boyu ile kirildiginda AL/SAT sinyali uretir
- Binance Futures verisi uzerinden calisir
"""
import os, time, logging
from datetime import datetime
import requests
import pandas as pd
import numpy as np
import urllib3, warnings

urllib3.disable_warnings()
warnings.filterwarnings('ignore')

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════
# AYARLAR
# ═══════════════════════════════════════════════════════════════
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
SCAN_INTERVAL = int(os.getenv("SCAN_INTERVAL", "300"))
MAX_COINS = int(os.getenv("MAX_COINS", "600"))
SIGNAL_COOLDOWN = int(os.getenv("SIGNAL_COOLDOWN", "3600"))
TIMEFRAME = os.getenv("TIMEFRAME", "5m")
KLINES_LIMIT = int(os.getenv("KLINES_LIMIT", "100"))

# OB & Breaker Strateji Ayarlari
SWING_LOOKBACK = int(os.getenv("SWING_LOOKBACK", "10"))
MIN_OVERLAP_PCT = float(os.getenv("MIN_OVERLAP_PCT", "30.0"))
MIN_BREAK_PCT = float(os.getenv("MIN_BREAK_PCT", "3.0"))
USE_BODY = os.getenv("USE_BODY", "false").lower() == "true"
SHOW_BULL = int(os.getenv("SHOW_BULL", "3"))
SHOW_BEAR = int(os.getenv("SHOW_BEAR", "3"))

BINANCE_BASE = "https://fapi.binance.com"
SESSION = requests.Session()
last_signal = {}

# ═══════════════════════════════════════════════════════════════
# FONKSIYONLAR
# ═══════════════════════════════════════════════════════════════

def get_symbols():
    try:
        r = SESSION.get(f"{BINANCE_BASE}/fapi/v1/exchangeInfo", timeout=10)
        data = r.json()
        syms = [s["symbol"] for s in data["symbols"]
                if s["symbol"].endswith("USDT") and s["status"] == "TRADING"]
        return syms[:MAX_COINS]
    except Exception as e:
        log.error(f"get_symbols hata: {e}")
        return []


def get_klines(symbol, interval, limit=500):
    try:
        r = SESSION.get(f"{BINANCE_BASE}/fapi/v1/klines",
                        params={"symbol": symbol, "interval": interval, "limit": limit},
                        timeout=10)
        raw = r.json()
        df = pd.DataFrame(raw, columns=[
            "open_time","open","high","low","close","volume","close_time",
            "qav","trades","tbv","tqv","ignore"])
        for c in ["open","high","low","close","volume"]:
            df[c] = df[c].astype(float)
        return df
    except Exception:
        return None


def detect_swings(df, length=10, use_body=False):
    if df is None or len(df) < length * 3:
        return None, None

    highs = df["high"].values if not use_body else np.maximum(df["open"].values, df["close"].values)
    lows = df["low"].values if not use_body else np.minimum(df["open"].values, df["close"].values)
    closes = df["close"].values
    n = len(df)

    swing_tops = []
    swing_btms = []
    os = 0

    for i in range(length, n - length):
        upper = np.max(highs[i-length:i])
        lower = np.min(lows[i-length:i])

        prev_os = os
        if highs[i] > upper:
            os = 0
        elif lows[i] < lower:
            os = 1

        if os == 0 and prev_os == 1:
            swing_tops.append((i, highs[i]))
        if os == 1 and prev_os == 0:
            swing_btms.append((i, lows[i]))

    return swing_tops, swing_btms


def find_order_blocks(df, swing_tops, swing_btms, use_body=False):
    if df is None or len(df) < 20:
        return [], []

    highs = df["high"].values if not use_body else np.maximum(df["open"].values, df["close"].values)
    lows = df["low"].values if not use_body else np.minimum(df["open"].values, df["close"].values)
    closes = df["close"].values

    bullish_obs = []
    bearish_obs = []

    # Bullish OB
    for idx, top_price in swing_tops:
        if idx >= len(df) - 1:
            continue
        if closes[idx + 1] > top_price:
            minima = highs[idx - 1]
            maxima = lows[idx - 1]
            loc = idx - 1

            for j in range(1, min(idx, 20)):
                if lows[idx - j] < minima:
                    minima = lows[idx - j]
                    maxima = highs[idx - j]
                    loc = idx - j

            bullish_obs.append({
                "top": float(maxima),
                "btm": float(minima),
                "loc": int(loc),
                "breaker": False,
                "break_loc": None,
                "break_price": None
            })

    # Bearish OB
    for idx, btm_price in swing_btms:
        if idx >= len(df) - 1:
            continue
        if closes[idx + 1] < btm_price:
            maxima = lows[idx - 1]
            minima = highs[idx - 1]
            loc = idx - 1

            for j in range(1, min(idx, 20)):
                if highs[idx - j] > maxima:
                    maxima = highs[idx - j]
                    minima = lows[idx - j]
                    loc = idx - j

            bearish_obs.append({
                "top": float(maxima),
                "btm": float(minima),
                "loc": int(loc),
                "breaker": False,
                "break_loc": None,
                "break_price": None
            })

    return bullish_obs, bearish_obs


def update_breaker_status(df, bullish_obs, bearish_obs):
    if df is None or len(df) < 2:
        return bullish_obs, bearish_obs

    closes = df["close"].values
    opens = df["open"].values
    highs = df["high"].values
    lows = df["low"].values

    for ob in bullish_obs:
        if not ob["breaker"]:
            if np.min(lows[max(0, ob["loc"]):]) < ob["btm"]:
                ob["breaker"] = True
                ob["break_loc"] = len(df) - 1
                ob["break_price"] = float(lows[-1])
        else:
            if closes[-1] > ob["top"]:
                ob["active"] = False

    for ob in bearish_obs:
        if not ob["breaker"]:
            if np.max(highs[max(0, ob["loc"]):]) > ob["top"]:
                ob["breaker"] = True
                ob["break_loc"] = len(df) - 1
                ob["break_price"] = float(highs[-1])
        else:
            if closes[-1] < ob["btm"]:
                ob["active"] = False

    bullish_obs = [ob for ob in bullish_obs if ob.get("active", True)]
    bearish_obs = [ob for ob in bearish_obs if ob.get("active", True)]

    return bullish_obs, bearish_obs


def calc_overlap(top1, btm1, top2, btm2):
    overlap_top = min(top1, top2)
    overlap_btm = max(btm1, btm2)
    overlap_size = max(0, overlap_top - overlap_btm)

    zone1_size = abs(top1 - btm1)
    zone2_size = abs(top2 - btm2)
    avg_size = (zone1_size + zone2_size) / 2

    if avg_size > 0:
        return (overlap_size / avg_size) * 100
    return 0


def find_confluences(bullish_obs, bearish_obs, min_overlap_pct=30.0):
    bull_confluences = []
    bear_confluences = []

    # Bull Confluence: Bull OB + Bear Breaker
    bull_breakers = [ob for ob in bearish_obs if ob["breaker"]]
    for bull_ob in bullish_obs[:SHOW_BULL]:
        for bear_br in bull_breakers[:SHOW_BEAR]:
            overlap = calc_overlap(bull_ob["top"], bull_ob["btm"], bear_br["top"], bear_br["btm"])
            if overlap >= min_overlap_pct:
                bull_confluences.append({
                    "top": min(bull_ob["top"], bear_br["top"]),
                    "btm": max(bull_ob["btm"], bear_br["btm"]),
                    "loc": bull_ob["loc"],
                    "is_bull": True,
                    "broken": False,
                    "break_bar": None,
                    "break_price": None,
                    "overlap_pct": overlap
                })

    # Bear Confluence: Bear OB + Bull Breaker
    bear_breakers = [ob for ob in bullish_obs if ob["breaker"]]
    for bear_ob in bearish_obs[:SHOW_BEAR]:
        for bull_br in bear_breakers[:SHOW_BULL]:
            overlap = calc_overlap(bear_ob["top"], bear_ob["btm"], bull_br["top"], bull_br["btm"])
            if overlap >= min_overlap_pct:
                bear_confluences.append({
                    "top": min(bear_ob["top"], bull_br["top"]),
                    "btm": max(bear_ob["btm"], bull_br["btm"]),
                    "loc": bear_ob["loc"],
                    "is_bull": False,
                    "broken": False,
                    "break_bar": None,
                    "break_price": None,
                    "overlap_pct": overlap
                })

    return bull_confluences, bear_confluences


def check_breaks(df, bull_confluences, bear_confluences, min_break_pct=3.0):
    if df is None or len(df) < 1:
        return [], []

    high = float(df["high"].iloc[-1])
    low = float(df["low"].iloc[-1])
    open_ = float(df["open"].iloc[-1])
    close = float(df["close"].iloc[-1])
    candle_body = abs(close - open_)
    candle_range = high - low

    bull_breaks = []
    bear_breaks = []

    # Bull Confluence: Asagi kirilim = BEARISH signal
    for conf in bull_confluences:
        if not conf["broken"]:
            zone_height = conf["top"] - conf["btm"]
            if zone_height <= 0:
                continue

            body_pct = (candle_body / zone_height) * 100
            range_pct = (candle_range / zone_height) * 100

            if low < conf["btm"] and (body_pct >= min_break_pct or range_pct >= min_break_pct):
                conf["broken"] = True
                conf["break_bar"] = len(df) - 1
                conf["break_price"] = low
                bull_breaks.append(conf)

    # Bear Confluence: Yukari kirilim = BULLISH signal
    for conf in bear_confluences:
        if not conf["broken"]:
            zone_height = conf["top"] - conf["btm"]
            if zone_height <= 0:
                continue

            body_pct = (candle_body / zone_height) * 100
            range_pct = (candle_range / zone_height) * 100

            if high > conf["top"] and (body_pct >= min_break_pct or range_pct >= min_break_pct):
                conf["broken"] = True
                conf["break_bar"] = len(df) - 1
                conf["break_price"] = high
                bear_breaks.append(conf)

    return bull_breaks, bear_breaks


def should_send(symbol, direction):
    key = f"{symbol}_{direction}"
    now = time.time()
    if now - last_signal.get(key, 0) < SIGNAL_COOLDOWN:
        return False
    last_signal[key] = now
    return True


def send_telegram(text):
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            data={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"},
            timeout=10
        )
        return r.status_code == 200
    except Exception as e:
        log.error(f"Telegram hata: {e}")
        return False


def format_message(symbol, direction, price, conf):
    emoji = "🔴 OB+BEAR BREAKER CONFLUENCE SAT" if direction == "SAT" else "🟢 OB+BULL BREAKER CONFLUENCE AL"
    coin = symbol.replace("USDT", "/USDT")
    sep = "═" * 20

    zone_height_pct = ((conf["top"] - conf["btm"]) / conf["btm"]) * 100

    lines = [
        f"{emoji}",
        sep,
        f"💱 Coin: {coin}",
        f"💰 Fiyat: {price:.6f}",
        f"📍 Confluence Zone: {conf['btm']:.6f} - {conf['top']:.6f}",
        f"📊 Zone Yüksekliği: %{zone_height_pct:.2f}",
        f"🔄 Overlap: %{conf['overlap_pct']:.1f}",
        f"📈 Break Mum Boyu: %{MIN_BREAK_PCT}",
        sep,
        f"⏰ {datetime.now().strftime('%H:%M:%S %d/%m/%Y')}",
    ]

    return "\n".join(lines)


def check_ob_confluence(symbol):
    df = get_klines(symbol, TIMEFRAME, limit=KLINES_LIMIT)
    if df is None or len(df) < SWING_LOOKBACK * 3:
        return None

    swing_tops, swing_btms = detect_swings(df, SWING_LOOKBACK, USE_BODY)
    if not swing_tops or not swing_btms:
        return None

    bullish_obs, bearish_obs = find_order_blocks(df, swing_tops, swing_btms, USE_BODY)
    if not bullish_obs and not bearish_obs:
        return None

    bullish_obs, bearish_obs = update_breaker_status(df, bullish_obs, bearish_obs)
    bull_confs, bear_confs = find_confluences(bullish_obs, bearish_obs, MIN_OVERLAP_PCT)
    bull_breaks, bear_breaks = check_breaks(df, bull_confs, bear_confs, MIN_BREAK_PCT)

    close = float(df["close"].iloc[-1])
    signals = []

    for conf in bull_breaks:
        if should_send(symbol, "SAT"):
            msg = format_message(symbol, "SAT", close, conf)
            signals.append({
                "direction": "SAT",
                "symbol": symbol,
                "price": close,
                "message": msg,
                "conf": conf
            })

    for conf in bear_breaks:
        if should_send(symbol, "AL"):
            msg = format_message(symbol, "AL", close, conf)
            signals.append({
                "direction": "AL",
                "symbol": symbol,
                "price": close,
                "message": msg,
                "conf": conf
            })

    return signals


def run_scan():
    symbols = get_symbols()
    log.info(f"OB & BREAKER CONFLUENCE TARAMA basladi | Coin: {len(symbols)} | TF: {TIMEFRAME} | MinBreak: %{MIN_BREAK_PCT}")
    found = 0

    for idx, symbol in enumerate(symbols):
        try:
            signals = check_ob_confluence(symbol)

            if signals:
                for sig in signals:
                    if send_telegram(sig["message"]):
                        found += 1
                        log.info(f"SINYAL {symbol} {sig['direction']} Fiyat:{sig['price']:.6f} Zone:{sig['conf']['btm']:.6f}-{sig['conf']['top']:.6f}")

            time.sleep(0.2)

        except Exception as e:
            log.error(f"{symbol} hata: {e}")
            continue

        if (idx + 1) % 50 == 0:
            log.info(f"[{idx+1}/{len(symbols)}] tarandi | {found} sinyal")

    log.info(f"Tarama tamamlandi | {found} sinyal gonderildi")
    return found


def main():
    log.info("OB & BREAKER CONFLUENCE BOT baslatildi")

    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log.error("TELEGRAM_TOKEN ve TELEGRAM_CHAT_ID eksik!")
        return

    send_telegram(
        f"🔍 OB & BREAKER CONFLUENCE BOT BASLADI\n"
        f"═" * 25 + "\n"
        f"💱 Zaman Dilimi: {TIMEFRAME}\n"
        f"📊 Swing Lookback: {SWING_LOOKBACK}\n"
        f"🔄 Min Overlap: %{MIN_OVERLAP_PCT}\n"
        f"📈 Min Break Mum: %{MIN_BREAK_PCT}\n"
        f"⏰ Tarama Araligi: {SCAN_INTERVAL}sn\n"
        f"🪙 Max Coin: {MAX_COINS}\n"
        f"\n"
        f"🔴 SAT: Bull OB + Bear Breaker confluence asagi kirilir\n"
        f"🟢 AL: Bear OB + Bull Breaker confluence yukari kirilir"
    )

    while True:
        try:
            run_scan()
        except Exception as e:
            log.error(f"run_scan genel hata: {e}")

        log.info(f"{SCAN_INTERVAL}sn bekleniyor...")
        time.sleep(SCAN_INTERVAL)


if __name__ == "__main__":
    main()
