# -*- coding: utf-8 -*-
"""
TREND KIRILIM BOT (15dk) - LuxAlgo "Trendlines with Breaks" portu
- Pivot high/low (14 bar) uzerinden ATR egimli trend cizgileri kurulur (Pine Script'teki
  upper/lower/slope_ph/slope_pl mantiginin birebir Python karsiligi).
- Fiyat, kirdigi trend cizgisinden en az MIN_BREAK_PCT (%5) uzaklastiysa sinyal uretilir.
- AL  = yukari kirilim (asagi yonlu trendi yukari kirdi)
- SAT = asagi kirilim (yukari yonlu trendi asagi kirdi)
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

# AYARLAR
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
SCAN_INTERVAL = int(os.getenv("SCAN_INTERVAL", "300"))
MAX_COINS = int(os.getenv("MAX_COINS", "600"))
SIGNAL_COOLDOWN = int(os.getenv("SIGNAL_COOLDOWN", "3600"))

TIMEFRAME = os.getenv("TIMEFRAME", "15m")
LENGTH = int(os.getenv("LENGTH", "14"))          # Pine: 'Swing Detection Lookback'
SLOPE_MULT = float(os.getenv("SLOPE_MULT", "1.0"))  # Pine: 'Slope'
MIN_BREAK_PCT = float(os.getenv("MIN_BREAK_PCT", "5.0"))  # min kirilim uzakligi %
KLINES_LIMIT = int(os.getenv("KLINES_LIMIT", "200"))

BINANCE_BASE = "https://fapi.binance.com"
SESSION = requests.Session()
last_signal = {}


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


def _atr(df, length):
    """Pine'in ta.atr'sine karsilik gelen Wilder/RMA tabanli ATR."""
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / length, min_periods=length, adjust=False).mean()


def _find_pivots(high, low, length):
    """ta.pivothigh(length,length) / ta.pivotlow(length,length) karsiligi.
    Donen diziler, pivotun PINE'daki gibi 'length' bar sonra onaylandigi ana hizalanmis
    (yani sinyal, pivot barindan 'length' bar sonraki indexte gorunur)."""
    n = len(high)
    ph_signal = np.full(n, np.nan)
    pl_signal = np.full(n, np.nan)

    for j in range(length, n - length):
        window_h = high[j - length:j + length + 1]
        if high[j] == window_h.max() and np.argmax(window_h) == length:
            ph_signal[j + length] = high[j]

        window_l = low[j - length:j + length + 1]
        if low[j] == window_l.min() and np.argmin(window_l) == length:
            pl_signal[j + length] = low[j]

    return ph_signal, pl_signal


def check_trend_break(df):
    """15dk CANLI mum uzerinden trend kirilim sinyali ara (Pine 'Trendlines with Breaks' portu)."""
    min_len = LENGTH * 3 + 5
    if df is None or len(df) < min_len:
        return None

    high = df["high"].values
    low = df["low"].values
    close = df["close"].values
    n = len(df)

    atr = _atr(df, LENGTH).values
    ph_signal, pl_signal = _find_pivots(high, low, LENGTH)

    upper = 0.0
    lower = 0.0
    slope_ph = 0.0
    slope_pl = 0.0
    upos = 0
    dnos = 0
    prev_upos = 0
    prev_dnos = 0

    # break_line_up / break_line_dn: kirilan trend cizgisinin o barda projeksiyon degeri
    break_line_up = np.nan
    break_line_dn = np.nan

    for i in range(n):
        is_ph = not np.isnan(ph_signal[i])
        is_pl = not np.isnan(pl_signal[i])

        slope_val = (atr[i] / LENGTH * SLOPE_MULT) if not np.isnan(atr[i]) else 0.0

        slope_ph = slope_val if is_ph else slope_ph
        slope_pl = slope_val if is_pl else slope_pl

        upper = ph_signal[i] if is_ph else upper - slope_ph
        lower = pl_signal[i] if is_pl else lower + slope_pl

        prev_upos, prev_dnos = upos, dnos

        line_up = upper - slope_ph * LENGTH
        line_dn = lower + slope_pl * LENGTH

        upos = 0 if is_ph else (1 if close[i] > line_up else upos)
        dnos = 0 if is_pl else (1 if close[i] < line_dn else dnos)

        if i == n - 1:
            break_line_up = line_up
            break_line_dn = line_dn

    price = float(close[-1])

    # AL: su an yukari kirilim durumunda VE cizgiden en az MIN_BREAK_PCT uzakta
    if upos == 1 and not np.isnan(break_line_up) and break_line_up > 0:
        break_pct = (price - break_line_up) / break_line_up * 100
        if break_pct >= MIN_BREAK_PCT:
            return {
                "direction": "AL",
                "price": round(price, 6),
                "break_pct": round(break_pct, 2),
                "trend_line": round(float(break_line_up), 6),
            }

    # SAT: su an asagi kirilim durumunda VE cizgiden en az MIN_BREAK_PCT uzakta
    if dnos == 1 and not np.isnan(break_line_dn) and break_line_dn > 0:
        break_pct = (break_line_dn - price) / break_line_dn * 100
        if break_pct >= MIN_BREAK_PCT:
            return {
                "direction": "SAT",
                "price": round(price, 6),
                "break_pct": round(break_pct, 2),
                "trend_line": round(float(break_line_dn), 6),
            }

    return None


def should_send(symbol, direction):
    key = f"{symbol}_{direction}"
    now = time.time()
    if now - last_signal.get(key, 0) < SIGNAL_COOLDOWN:
        return False
    last_signal[key] = now
    return True


def send_telegram(text):
    try:
        r = requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                          data={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"},
                          timeout=10)
        return r.status_code == 200
    except Exception as e:
        log.error(f"Telegram hata: {e}")
        return False


def format_message(symbol, sig):
    emoji = "TREND KIRILIM AL" if sig["direction"] == "AL" else "TREND KIRILIM SAT"
    coin = symbol.replace("USDT", "/USDT")
    sep = "=" * 16

    lines = [
        f"{emoji}",
        sep,
        f"Coin: {coin}",
        f"Fiyat: {sig['price']}",
        f"Trend Cizgisi: {sig['trend_line']}",
        f"Kirilim: %{sig['break_pct']}",
        sep,
        f"{datetime.now().strftime('%H:%M:%S %d/%m/%Y')}",
    ]

    return "\n".join(lines)


def run_scan():
    symbols = get_symbols()
    log.info(f"TREND KIRILIM TARAMA basladi Coin:{len(symbols)}")
    found = 0

    for idx, symbol in enumerate(symbols):
        try:
            df = get_klines(symbol, TIMEFRAME, limit=KLINES_LIMIT)
            sig = check_trend_break(df)

            if sig and should_send(symbol, sig["direction"]):
                if send_telegram(format_message(symbol, sig)):
                    found += 1
                    log.info(f"SINYAL {symbol} {sig['direction']} Fiyat:{sig['price']} Kirilim:%{sig['break_pct']}")

            time.sleep(0.2)

        except Exception as e:
            log.error(f"{symbol} hata: {e}")
            continue

        if (idx + 1) % 50 == 0:
            log.info(f"[{idx+1}/{len(symbols)}] tarandi {found} sinyal")

    log.info(f"Tarama tamamlandi {found} sinyal gonderildi")


def main():
    log.info("TREND KIRILIM BOT baslatildi")

    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log.error("TELEGRAM_TOKEN ve TELEGRAM_CHAT_ID eksik!")
        return

    send_telegram(
        f"TREND KIRILIM BOT BASLADI\n"
        f"Zaman Dilimi: {TIMEFRAME}\n"
        f"Swing Lookback: {LENGTH} | Slope Carpani: {SLOPE_MULT}\n"
        f"Min Kirilim: %{MIN_BREAK_PCT}\n"
        f"Tarama Araligi: {SCAN_INTERVAL}sn | Coin: {MAX_COINS}"
    )

    while True:
        try:
            run_scan()
        except Exception as e:
            log.error(f"run_scan genel hata: {e}")
        time.sleep(SCAN_INTERVAL)


if __name__ == "__main__":
    main()
