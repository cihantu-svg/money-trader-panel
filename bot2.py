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
TIMEFRAME = os.getenv("SCAN_TIMEFRAME", "1h")
MAX_COINS = int(os.getenv("MAX_COINS", "200"))

RSI_LEN = int(os.getenv("RSI_LEN", "14"))
RSI_EMA_LEN = int(os.getenv("RSI_EMA_LEN", "10"))
MOM_LEN = int(os.getenv("MOM_LEN", "10"))
VOL_MA_LEN = int(os.getenv("VOL_MA_LEN", "20"))
VOL_MULT_UP = float(os.getenv("VOL_MULT_UP", "1.5"))
VOL_MULT_DOWN = float(os.getenv("VOL_MULT_DOWN", "0.7"))
RSI_LEVEL = float(os.getenv("RSI_LEVEL", "50"))
FIB_LEN = int(os.getenv("FIB_LEN", "100"))
SIGNAL_COOLDOWN = int(os.getenv("SIGNAL_COOLDOWN", "3600"))

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

def check_signal(df):
    if df is None or len(df) < FIB_LEN + 20:
        return None

    # Son satir olusmakta olan (kapanmamis) mum -> repaint'i onlemek icin at
    df = df.iloc[:-1]
    if len(df) < FIB_LEN + 5:
        return None

    close = df["close"]
    volume = df["volume"]

    rsi_val = rsi(close, RSI_LEN)
    rsi_ema = ema(rsi_val, RSI_EMA_LEN)
    mom_val = close - close.shift(MOM_LEN)
    vol_ma = volume.rolling(VOL_MA_LEN).mean()

    rsi_top = rsi_val.rolling(FIB_LEN).max()
    rsi_bot = rsi_val.rolling(FIB_LEN).min()
    fib_500 = rsi_top - (rsi_top - rsi_bot) * 0.5

    i, p = -1, -2  # i: son KAPANMIS mum, p: onceki mum
    rsi_now, rsi_prev = rsi_val.iloc[i], rsi_val.iloc[p]
    ema_now, ema_prev = rsi_ema.iloc[i], rsi_ema.iloc[p]
    mom_now = mom_val.iloc[i]
    vol_now, vol_ma_now = volume.iloc[i], vol_ma.iloc[i]
    fib_prev = fib_500.iloc[p]  # Pine'daki fib_500[1]

    if pd.isna(rsi_now) or pd.isna(vol_ma_now) or pd.isna(fib_prev):
        return None

    # === GRUP 1: Hacim + Momentum (RSI 50 kesimi) ===
    trigger_al = (vol_now > vol_ma_now * VOL_MULT_UP) and (rsi_prev <= RSI_LEVEL and rsi_now > RSI_LEVEL)
    trigger_sat = (vol_now < vol_ma_now * VOL_MULT_DOWN) and (rsi_prev >= RSI_LEVEL and rsi_now < RSI_LEVEL)

    # === GRUP 2: AL / AL-Fibo  ve  SAT / SAT-Fibo ===
    mom_bull = mom_now > 0
    mom_bear = mom_now < 0
    vol_high = vol_now > vol_ma_now

    rsi_cross_up = rsi_prev <= ema_prev and rsi_now > ema_now
    fibo_al = rsi_now > fib_prev and rsi_prev <= fib_prev
    confluence_al = (rsi_cross_up and mom_bull and vol_high) or (fibo_al and mom_bull and vol_high)

    rsi_cross_down = rsi_prev >= ema_prev and rsi_now < ema_now
    fibo_sat = rsi_now < fib_prev and rsi_prev >= fib_prev
        confluence_sat = (rsi_cross_down and mom_bear and vol_high) or (fibo_sat and mom_bear and vol_high)
  
    # === CONFLUENCE: iki grup ayni mumda ===
    if trigger_al and confluence_al:
        return {"direction": "AL", "price": close.iloc[i], "rsi": round(float(rsi_now), 2)}
    if trigger_sat and confluence_sat:
        return {"direction": "SAT", "price": close.iloc[i], "rsi": round(float(rsi_now), 2)}
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
    sep = "\u2501" * 12
    lines = [
        f"{emoji} <b>HACIM+MOMENTUM & FIBO ONAYLI {sig['direction']}</b>",
        sep,
        f"\U0001F4CD <b>{coin}</b>",
        f"\u23F1 Zaman Dilimi: <b>{TIMEFRAME}</b>",
        f"\U0001F4B0 Fiyat: <b>{sig['price']}</b>",
        f"\U0001F4CA RSI: <b>{sig['rsi']}</b>",
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
