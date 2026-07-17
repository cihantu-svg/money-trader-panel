# -*- coding: utf-8 -*-
"""
30dk CANLI + 5dk HACIMLI %3 KIRILIM BOT
- 30dk CANLI mum uzerinden tepe/dip/major bolge hesaplar (repaint riski var ama erken sinyal)
- 5dk grafikte o bolgeden %3+ yukari/asagi kirilim + guclu hacim arar
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

# 30dk bolge ayarlari (CANLI)
ZONE_TIMEFRAME = "30m"
AUTO_ZONE_DAYS = int(os.getenv("AUTO_ZONE_DAYS", "50"))
AUTO_MAJOR_BINS = int(os.getenv("AUTO_MAJOR_BINS", "10"))
AUTO_PEAK_PCT = float(os.getenv("AUTO_PEAK_PCT", "3.0"))
AUTO_DIP_PCT = float(os.getenv("AUTO_DIP_PCT", "3.0"))

# 5dk kirilim ayarlari
BREAK_TIMEFRAME = "5m"
MIN_CANDLE_PCT = float(os.getenv("MIN_CANDLE_PCT", "3.0"))
VOL_MULT = float(os.getenv("VOL_MULT", "3.0"))  # 3x ve uzeri guclu hacim

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


def calculate_zones_live(df_30m):
    """30dk CANLI mum dahil bolge hesapla"""
    if df_30m is None or len(df_30m) < AUTO_ZONE_DAYS:
        return None

    # CANLI mum dahil! Son mum atilmiyor
    df = df_30m
    lookback = min(AUTO_ZONE_DAYS, len(df))

    # TEPENIN BELIRLENMESI (CANLI)
    auto_highest = df["high"].iloc[-lookback:].max()
    auto_lowest = df["low"].iloc[-lookback:].min()

    peak_top = auto_highest
    peak_bot = auto_highest * (1 - AUTO_PEAK_PCT / 100)

    # DIPIN BELIRLENMESI (CANLI)
    dip_top = auto_lowest * (1 + AUTO_DIP_PCT / 100)
    dip_bot = auto_lowest

    # MAJOR BOLGE (Histogram - CANLI)
    price_range = auto_highest - auto_lowest
    if price_range <= 0 or AUTO_MAJOR_BINS <= 0:
        return None

    step = price_range / AUTO_MAJOR_BINS
    bins = [0] * AUTO_MAJOR_BINS

    for price in df["close"].iloc[-lookback:]:
        idx = min(AUTO_MAJOR_BINS - 1, max(0, int((price - auto_lowest) / step)))
        bins[idx] += 1

    max_bin = 0
    max_val = bins[0]
    for j in range(1, AUTO_MAJOR_BINS):
        if bins[j] > max_val:
            max_val = bins[j]
            max_bin = j

    major_top = auto_lowest + step * (max_bin + 1)
    major_bot = auto_lowest + step * max_bin

    return {
        "peak": {"top": float(peak_top), "bot": float(peak_bot)},
        "dip": {"top": float(dip_top), "bot": float(dip_bot)},
        "major": {"top": float(major_top), "bot": float(major_bot)},
        "highest": float(auto_highest),
        "lowest": float(auto_lowest)
    }


def check_breakout_live(df_5m, zones, symbol):
    """5dk CANLI mum dahil kirilim ara"""
    if df_5m is None or len(df_5m) < 30 or zones is None:
        return None

    # CANLI mum dahil! Son mum atilmiyor
    df = df_5m
    if len(df) < 2:
        return None

    i = -1   # Son mum (CANLI)
    p = -2   # Onceki mum

    close_now = df["close"].iloc[i]
    close_prev = df["close"].iloc[p]
    open_now = df["open"].iloc[i]
    volume_now = df["volume"].iloc[i]

    # Mum boyu %
    candle_pct = abs(close_now - open_now) / open_now * 100

    # Hacim ortalamasi (son 20 mum)
    vol_sma20 = df["volume"].rolling(20).mean().iloc[i]
    vol_ratio = volume_now / vol_sma20 if vol_sma20 > 0 else 0
    vol_ok = vol_ratio >= VOL_MULT  # 3x ve uzeri

    if not vol_ok or candle_pct < MIN_CANDLE_PCT:
        return None

    # Bolgeler
    peak = zones["peak"]
    dip = zones["dip"]
    major = zones["major"]

    # YUKARI KIRILIM (AL) - CANLI
    prev_below_peak = close_prev <= peak["top"]
    now_above_peak_break = close_now >= peak["top"] * (1 + MIN_CANDLE_PCT / 100)

    prev_below_major = close_prev <= major["top"]
    now_above_major_break = close_now >= major["top"] * (1 + MIN_CANDLE_PCT / 100)

    prev_below_dip = close_prev <= dip["top"]
    now_above_dip_break = close_now >= dip["top"] * (1 + MIN_CANDLE_PCT / 100)

    if prev_below_peak and now_above_peak_break:
        return {
            "direction": "AL",
            "type": "TEPE KIRILIM",
            "price": round(float(close_now), 4),
            "candle_pct": round(float(candle_pct), 2),
            "vol_ratio": round(float(vol_ratio), 2),
            "zone": f"{round(peak['bot'], 4)} - {round(peak['top'], 4)}",
            "break_level": round(float(peak["top"] * (1 + MIN_CANDLE_PCT / 100)), 4)
        }

    if prev_below_major and now_above_major_break:
        return {
            "direction": "AL",
            "type": "MAJOR KIRILIM",
            "price": round(float(close_now), 4),
            "candle_pct": round(float(candle_pct), 2),
            "vol_ratio": round(float(vol_ratio), 2),
            "zone": f"{round(major['bot'], 4)} - {round(major['top'], 4)}",
            "break_level": round(float(major["top"] * (1 + MIN_CANDLE_PCT / 100)), 4)
        }

    if prev_below_dip and now_above_dip_break:
        return {
            "direction": "AL",
            "type": "DIP KIRILIM",
            "price": round(float(close_now), 4),
            "candle_pct": round(float(candle_pct), 2),
            "vol_ratio": round(float(vol_ratio), 2),
            "zone": f"{round(dip['bot'], 4)} - {round(dip['top'], 4)}",
            "break_level": round(float(dip["top"] * (1 + MIN_CANDLE_PCT / 100)), 4)
        }

    # ASAGI KIRILIM (SAT) - CANLI
    prev_above_peak = close_prev >= peak["bot"]
    now_below_peak_break = close_now <= peak["bot"] * (1 - MIN_CANDLE_PCT / 100)

    prev_above_major = close_prev >= major["bot"]
    now_below_major_break = close_now <= major["bot"] * (1 - MIN_CANDLE_PCT / 100)

    prev_above_dip = close_prev >= dip["bot"]
    now_below_dip_break = close_now <= dip["bot"] * (1 - MIN_CANDLE_PCT / 100)

    if prev_above_peak and now_below_peak_break:
        return {
            "direction": "SAT",
            "type": "TEPE KIRILIM",
            "price": round(float(close_now), 4),
            "candle_pct": round(float(candle_pct), 2),
            "vol_ratio": round(float(vol_ratio), 2),
            "zone": f"{round(peak['bot'], 4)} - {round(peak['top'], 4)}",
            "break_level": round(float(peak["bot"] * (1 - MIN_CANDLE_PCT / 100)), 4)
        }

    if prev_above_major and now_below_major_break:
        return {
            "direction": "SAT",
            "type": "MAJOR KIRILIM",
            "price": round(float(close_now), 4),
            "candle_pct": round(float(candle_pct), 2),
            "vol_ratio": round(float(vol_ratio), 2),
            "zone": f"{round(major['bot'], 4)} - {round(major['top'], 4)}",
            "break_level": round(float(major["bot"] * (1 - MIN_CANDLE_PCT / 100)), 4)
        }

    if prev_above_dip and now_below_dip_break:
        return {
            "direction": "SAT",
            "type": "DIP KIRILIM",
            "price": round(float(close_now), 4),
            "candle_pct": round(float(candle_pct), 2),
            "vol_ratio": round(float(vol_ratio), 2),
            "zone": f"{round(dip['bot'], 4)} - {round(dip['top'], 4)}",
            "break_level": round(float(dip["bot"] * (1 - MIN_CANDLE_PCT / 100)), 4)
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
    emoji = "RO" if sig["direction"] == "AL" else "SAT"
    coin = symbol.replace("USDT", "/USDT")
    sep = "=" * 16

    lines = [
        f"{emoji} {sig['type']} - {sig['direction']}",
        sep,
        f"Coin: {coin}",
        f"Bolge: {sig['zone']}",
        f"Fiyat: {sig['price']}",
        f"Kirilim: {sig['break_level']}",
        f"Mum Boyu: %{sig['candle_pct']}",
        f"Hacim: {sig['vol_ratio']}x ort (GUCU)",
        sep,
        f"{datetime.now().strftime('%H:%M:%S %d/%m/%Y')}",
    ]

    return "\n".join(lines)


def run_scan():
    symbols = get_symbols()
    log.info(f"30dk CANLI + 5dk HACIMLI %3 KIRILIM TARAMA basladi Coin:{len(symbols)}")
    found = 0

    for idx, symbol in enumerate(symbols):
        try:
            # 30dk CANLI bolge belirle
            df_30m = get_klines(symbol, ZONE_TIMEFRAME, limit=500)
            zones = calculate_zones_live(df_30m)

            if zones is None:
                continue

            # 5dk CANLI kirilim ara
            df_5m = get_klines(symbol, BREAK_TIMEFRAME, limit=100)
            sig = check_breakout_live(df_5m, zones, symbol)

            if sig and should_send(symbol, sig["direction"]):
                if send_telegram(format_message(symbol, sig)):
                    found += 1
                    log.info(f"SINYAL {symbol} {sig['type']} {sig['direction']} Fiyat:{sig['price']} Vol:{sig['vol_ratio']}x")

            time.sleep(0.2)

        except Exception as e:
            log.error(f"{symbol} hata: {e}")
            continue

        if (idx + 1) % 50 == 0:
            log.info(f"[{idx+1}/{len(symbols)}] tarandi {found} sinyal")

    log.info(f"Tarama tamamlandi {found} sinyal gonderildi")


def main():
    log.info("30dk CANLI + 5dk HACIMLI %3 KIRILIM BOT baslatildi")

    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log.error("TELEGRAM_TOKEN ve TELEGRAM_CHAT_ID eksik!")
        return

    send_telegram(
        f"30dk CANLI + 5dk HACIMLI %3 KIRILIM BOT BASLADI\n"
        f"Bolge TF: {ZONE_TIMEFRAME} (CANLI)\n"
        f"Kirilim TF: {BREAK_TIMEFRAME} (CANLI)\n"
        f"Min Mum: %{MIN_CANDLE_PCT}\n"
        f"Hacim: {VOL_MULT}x ort (GUCU)\n"
        f"UYARI: Canli mum = Repaint riski var!"
    )

    while True:
        try:
            run_scan()
        except Exception as e:
            log.error(f"run_scan genel hata: {e}")
        time.sleep(SCAN_INTERVAL)


if __name__ == "__main__":
    main()
