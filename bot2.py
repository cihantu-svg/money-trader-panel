# -*- coding: utf-8 -*-
"""
HACIM + MOMENTUM BOT (5dk)
- AL: Hacim, 20 periyot ortalamasinin VOL_MULT_UP kati ve uzerinde + RSI 50'yi yukari kesti
- SAT: Hacim, 20 periyot ortalamasinin VOL_MULT_DOWN kati ve uzerinde + RSI 50'yi asagi kesti
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

TIMEFRAME = os.getenv("TIMEFRAME", "5m")
VOL_MA_LEN = int(os.getenv("VOL_MA_LEN", "20"))
RSI_LEN = int(os.getenv("RSI_LEN", "14"))
RSI_LEVEL = float(os.getenv("RSI_LEVEL", "50"))
VOL_MULT_UP = float(os.getenv("VOL_MULT_UP", "5.0"))    # AL: hacim >= ortalamanin 5 kati
VOL_MULT_DOWN = float(os.getenv("VOL_MULT_DOWN", "3.0"))  # SAT: hacim >= ortalamanin 3 kati (AL'dan farkli esik)
KLINES_LIMIT = int(os.getenv("KLINES_LIMIT", "100"))

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


def _rsi(series, length):
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / length, min_periods=length, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / length, min_periods=length, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    out = 100 - (100 / (1 + rs))
    return out.fillna(50)


def check_vol_momentum(df):
    """5dk CANLI mum uzerinden Hacim+Momentum AL/SAT ara.
    AL: hacim >= VOL_MULT_UP x ortalama + RSI 50 yukari kesisim
    SAT: hacim >= VOL_MULT_DOWN x ortalama + RSI 50 asagi kesisim"""
    if df is None or len(df) < VOL_MA_LEN + RSI_LEN + 5:
        return None

    close, volume = df["close"], df["volume"]
    vol_sma = volume.rolling(VOL_MA_LEN).mean()
    vol_ratio = volume / vol_sma
    rsi_val = _rsi(close, RSI_LEN)

    i, p = -1, -2  # son (CANLI) mum / bir onceki mum

    if pd.isna(vol_ratio.iloc[i]) or pd.isna(rsi_val.iloc[i]) or pd.isna(rsi_val.iloc[p]):
        return None

    vol_ratio_now = float(vol_ratio.iloc[i])
    rsi_now = float(rsi_val.iloc[i])
    rsi_prev = float(rsi_val.iloc[p])
    price = float(close.iloc[i])

    # AL: hacim guclu artis + RSI 50 yukari kesisim
    if vol_ratio_now >= VOL_MULT_UP and rsi_prev <= RSI_LEVEL < rsi_now:
        return {
            "direction": "AL",
            "price": round(price, 6),
            "vol_ratio": round(vol_ratio_now, 2),
            "rsi": round(rsi_now, 1),
        }

    # SAT: hacim guclu artis (ayri esik) + RSI 50 asagi kesisim
    if vol_ratio_now >= VOL_MULT_DOWN and rsi_prev >= RSI_LEVEL > rsi_now:
        return {
            "direction": "SAT",
            "price": round(price, 6),
            "vol_ratio": round(vol_ratio_now, 2),
            "rsi": round(rsi_now, 1),
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
    emoji = "HACIM+MOMENTUM AL" if sig["direction"] == "AL" else "HACIM DUSUS+MOMENTUM KAYBI SAT"
    coin = symbol.replace("USDT", "/USDT")
    sep = "=" * 16

    lines = [
        f"{emoji}",
        sep,
        f"Coin: {coin}",
        f"Fiyat: {sig['price']}",
        f"Hacim: {sig['vol_ratio']}x ortalama",
        f"RSI: {sig['rsi']}",
        sep,
        f"{datetime.now().strftime('%H:%M:%S %d/%m/%Y')}",
    ]

    return "\n".join(lines)


def run_scan():
    symbols = get_symbols()
    log.info(f"HACIM+MOMENTUM TARAMA basladi Coin:{len(symbols)}")
    found = 0

    for idx, symbol in enumerate(symbols):
        try:
            df = get_klines(symbol, TIMEFRAME, limit=KLINES_LIMIT)
            sig = check_vol_momentum(df)

            if sig and should_send(symbol, sig["direction"]):
                if send_telegram(format_message(symbol, sig)):
                    found += 1
                    log.info(f"SINYAL {symbol} {sig['direction']} Fiyat:{sig['price']} Vol:{sig['vol_ratio']}x RSI:{sig['rsi']}")

            time.sleep(0.2)

        except Exception as e:
            log.error(f"{symbol} hata: {e}")
            continue

        if (idx + 1) % 50 == 0:
            log.info(f"[{idx+1}/{len(symbols)}] tarandi {found} sinyal")

    log.info(f"Tarama tamamlandi {found} sinyal gonderildi")


def main():
    log.info("HACIM+MOMENTUM BOT baslatildi")

    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log.error("TELEGRAM_TOKEN ve TELEGRAM_CHAT_ID eksik!")
        return

    send_telegram(
        f"HACIM+MOMENTUM BOT BASLADI\n"
        f"Zaman Dilimi: {TIMEFRAME}\n"
        f"AL: Hacim >= {VOL_MULT_UP}x ortalama + RSI {RSI_LEVEL} yukari kesisim\n"
        f"SAT: Hacim >= {VOL_MULT_DOWN}x ortalama + RSI {RSI_LEVEL} asagi kesisim\n"
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
