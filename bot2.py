# -*- coding: utf-8 -*-
"""
HACIM+MOMENTUM & RSI-FIBO CONFLUENCE BOT
Pine 'MONEY TRADER - FULL PAKET (CONFLUENCE)' mantigi birebir portlandi.
  AL  = (Hacim yukselisi + RSI 50 yukari kesim)  VE  (RSI kendi EMA'sini VEYA orta-Fibo'yu yukari kesim)
  SAT = (Hacim dususu + RSI 50 asagi kesim)       VE  (RSI kendi EMA'sini VEYA orta-Fibo'yu asagi kesim)
  Iki grup da AYNI (KAPANMIS) mumda saglanirsa sinyal gonderilir (repaint yok).
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

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
SCAN_INTERVAL = int(os.getenv("SCAN_INTERVAL", "300"))
TIMEFRAME = os.getenv("SCAN_TIMEFRAME", "15m")
MAX_COINS = int(os.getenv("MAX_COINS", "600"))

RSI_LEN = int(os.getenv("RSI_LEN", "14"))
RSI_EMA_LEN = int(os.getenv("RSI_EMA_LEN", "10"))
MOM_LEN = int(os.getenv("MOM_LEN", "10"))
VOL_MA_LEN = int(os.getenv("VOL_MA_LEN", "20"))
VOL_MULT_UP = float(os.getenv("VOL_MULT_UP", "1.5"))
VOL_MULT_DOWN = float(os.getenv("VOL_MULT_DOWN", "0.7"))
RSI_LEVEL = float(os.getenv("RSI_LEVEL", "50"))
FIB_LEN = int(os.getenv("FIB_LEN", "100"))
SIGNAL_COOLDOWN = int(os.getenv("SIGNAL_COOLDOWN", "3600"))

# YENI: Major Bolge ayarlari
AUTO_ZONE_DAYS = int(os.getenv("AUTO_ZONE_DAYS", "50"))
AUTO_MAJOR_BINS = int(os.getenv("AUTO_MAJOR_BINS", "10"))
MAJOR_BREAK_PCT = float(os.getenv("MAJOR_BREAK_PCT", "4.0"))

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

def get_klines(symbol, limit=300):
    try:
        r = SESSION.get(f"{BINANCE_BASE}/fapi/v1/klines",
                        params={"symbol": symbol, "interval": TIMEFRAME, "limit": limit},
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

def rsi(series, length):
    delta = series.diff()
    up = delta.clip(lower=0)
    down = -delta.clip(upper=0)
    ma_up = up.ewm(alpha=1/length, adjust=False).mean()
    ma_down = down.ewm(alpha=1/length, adjust=False).mean()
    rs = ma_up / ma_down.replace(0, np.nan)
    return 100 - (100 / (1 + rs))

def ema(series, length):
    return series.ewm(span=length, adjust=False).mean()



def calculate_major_zone(df):
    if df is None or len(df) < AUTO_ZONE_DAYS + 10:
        return None, None
    df_closed = df.iloc[:-1]
    if len(df_closed) < AUTO_ZONE_DAYS:
        return None, None
    close = df_closed["close"]
    lookback = min(AUTO_ZONE_DAYS, len(df_closed))
    auto_highest = close.iloc[-lookback:].max()
    auto_lowest = close.iloc[-lookback:].min()
    price_range = auto_highest - auto_lowest
    if price_range <= 0 or AUTO_MAJOR_BINS <= 0:
        return None, None
    step = price_range / AUTO_MAJOR_BINS
    bins = [0] * AUTO_MAJOR_BINS
    for price in close.iloc[-lookback:]:
        idx = min(AUTO_MAJOR_BINS - 1, max(0, int((price - auto_lowest) / step)))
        bins[idx] += 1
    max_bin = 0
    max_val = bins[0]
    for j in range(1, AUTO_MAJOR_BINS):
        if bins[j] > max_val:
            max_val = bins[j]
            max_bin = j
    major_bot = auto_lowest + step * max_bin
    major_top = auto_lowest + step * (max_bin + 1)
    return float(major_bot), float(major_top)


def check_signal(df):
    if df is None or len(df) < AUTO_ZONE_DAYS + 20:
        return None
    major_bot, major_top = calculate_major_zone(df)
    if major_bot is None or major_top is None:
        return None
    df_closed = df.iloc[:-1]
    if len(df_closed) < 2:
        return None
    i, p = -1, -2
    close_now = df_closed["close"].iloc[i]
    close_prev = df_closed["close"].iloc[p]
    break_up_threshold = major_top * (1 + MAJOR_BREAK_PCT / 100)
    break_dn_threshold = major_bot * (1 - MAJOR_BREAK_PCT / 100)
    prev_below_major = close_prev <= major_top
    now_above_break = close_now >= break_up_threshold
    prev_above_major = close_prev >= major_bot
    now_below_break = close_now <= break_dn_threshold
    if prev_below_major and now_above_break:
        return {"direction": "AL", "price": round(float(close_now), 4), "major_zone": f"{round(major_bot, 4)} - {round(major_top, 4)}", "break_level": round(float(break_up_threshold), 4)}
    if prev_above_major and now_below_break:
        return {"direction": "SAT", "price": round(float(close_now), 4), "major_zone": f"{round(major_bot, 4)} - {round(major_top, 4)}", "break_level": round(float(break_dn_threshold), 4)}
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
    emoji = "\U0001F7E2" if sig["direction"] == "AL" else "\U0001F534"
    coin = symbol.replace("USDT", "/USDT")
    sep = "\u2501" * 14
    lines = [
        f"{emoji} <b>MAJOR BOLGE %4 KIRILIM - {sig['direction']}</b>",
        sep,
        f"\U0001F4CD <b>{coin}</b>",
        f"\u23F1 Zaman Dilimi: <b>{TIMEFRAME}</b>",
        f"\U0001F4B0 Fiyat: <b>{sig['price']}</b>",
        f"\U0001F3D7 Major Bolge: <b>{sig['major_zone']}</b>",
        f"\U0001F6A9 Kirilim Seviyesi: <b>{sig['break_level']}</b>",
        sep,
        f"\U0001F551 {datetime.now().strftime('%H:%M:%S %d/%m/%Y')}",
    ]
    return "\n".join(lines)
def run_scan():
    symbols = get_symbols()
    log.info(f"Tarama basladi TF:{TIMEFRAME} Coin:{len(symbols)}")
    found = 0
    for idx, symbol in enumerate(symbols):
        try:
            df = get_klines(symbol)
            sig = check_signal(df)
            if sig and should_send(symbol, sig["direction"]):
                if send_telegram(format_message(symbol, sig)):
                    found += 1
                    log.info(f"OK {symbol} {sig['direction']} rsi:{sig['rsi']}")
            time.sleep(0.1)
        except Exception as e:
            log.error(f"{symbol} hata: {e}")
            continue
        if (idx + 1) % 50 == 0:
            log.info(f"[{idx+1}/{len(symbols)}] tarandi {found} sinyal")
    log.info(f"Tarama tamamlandi {found} sinyal gonderildi")

def main():
    log.info("HACIM+MOMENTUM & FIBO CONFLUENCE BOT baslatildi")
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log.error("TELEGRAM_TOKEN ve TELEGRAM_CHAT_ID eksik!")
        return
    send_telegram(f"HACIM+MOMENTUM & FIBO CONFLUENCE BOT BASLADI\nZaman dilimi: {TIMEFRAME}")
    while True:
        try:
            run_scan()
        except Exception as e:
            log.error(f"run_scan genel hata: {e}")
        time.sleep(SCAN_INTERVAL)

if __name__ == "__main__":
    main()
