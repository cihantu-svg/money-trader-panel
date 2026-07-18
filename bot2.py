# -*- coding: utf-8 -*-
"""
ROKET AL / ROKET SAT BOT
- 15dk grafikte Heikin Ashi tabanli Quantum Golden (COK GUCLU) donusu
  + Major Level (SMA) kesisimi
  iki kosul ayni anda gerceklestiginde ROKET AL / ROKET SAT gonderir.
- CANLI mum uzerinden calisir (repaint riski var, erken sinyal icin bilinçli tercih).
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

# ROKET AL/SAT ayarlari (hepsinin varsayilani var, env eklemeden calisir)
ROKET_TIMEFRAME = os.getenv("ROKET_TIMEFRAME", "15m")
ROKET_HASSASIYET = float(os.getenv("ROKET_HASSASIYET", "0.1"))
ROKET_ATR_PERIOD = int(os.getenv("ROKET_ATR_PERIOD", "14"))
ROKET_MAJOR_LEN = int(os.getenv("ROKET_MAJOR_LEN", "100"))
ROKET_MAJOR_BREAK_PCT = float(os.getenv("ROKET_MAJOR_BREAK_PCT", "1.0"))
ROKET_KLINES_LIMIT = int(os.getenv("ROKET_KLINES_LIMIT", "150"))

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


# ════════════════════════════════════════════════════════════════
# ROKET AL / ROKET SAT hesaplama (Quantum Golden COK GUCLU + Major Level kesisimi)
# ════════════════════════════════════════════════════════════════
def _atr(df, length):
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / length, min_periods=length, adjust=False).mean()


def _heikin_ashi(df):
    ha_close = (df["open"] + df["high"] + df["low"] + df["close"]) / 4
    ha_open = np.empty(len(df))
    ha_open[0] = (df["open"].iloc[0] + df["close"].iloc[0]) / 2
    ha_close_vals = ha_close.values
    for i in range(1, len(df)):
        ha_open[i] = (ha_open[i - 1] + ha_close_vals[i - 1]) / 2
    ha_open = pd.Series(ha_open, index=df.index)
    ha_high = pd.concat([df["high"], ha_open, ha_close], axis=1).max(axis=1)
    ha_low = pd.concat([df["low"], ha_open, ha_close], axis=1).min(axis=1)
    return ha_open, ha_high, ha_low, ha_close


def check_roket_live(df_roket):
    """15dk CANLI mum uzerinden ROKET AL / ROKET SAT ara.
    Kosul: Quantum Golden COK GUCLU donusu + ayni anda Major Level (turuncu cizgi) kesisimi."""
    min_len = ROKET_MAJOR_LEN + 15
    if df_roket is None or len(df_roket) < min_len:
        return None

    df = df_roket
    close = df["close"]

    # --- Major Level (turuncu SMA cizgisi) ve kesisim ---
    major_level = close.rolling(ROKET_MAJOR_LEN).mean()
    dist_from_major = (close - major_level).abs() / major_level * 100
    major_break_up = (close > major_level) & (dist_from_major >= ROKET_MAJOR_BREAK_PCT)
    major_break_down = (close < major_level) & (dist_from_major >= ROKET_MAJOR_BREAK_PCT)
    crossed_up = (close.shift(1) <= major_level.shift(1)) & major_break_up
    crossed_down = (close.shift(1) >= major_level.shift(1)) & major_break_down

    # --- Heikin Ashi / Quantum Golden (AL + ayna SAT) ---
    ha_open, ha_high, ha_low, ha_close = _heikin_ashi(df)
    govde_degisim = (ha_close - ha_open) / ha_open * 100
    mutlak_degisim = govde_degisim.abs()
    avg_body = mutlak_degisim.rolling(10).mean()
    sinyal_gucu = (mutlak_degisim / avg_body.replace(0, np.nan) * 50).clip(upper=100)
    olasilik = 65 + sinyal_gucu / 4 + np.where(mutlak_degisim > ROKET_HASSASIYET * 2.5, 12, 0)
    olasilik_son = np.minimum(98, olasilik)

    ha_is_up = ha_close > ha_open
    ha_is_down = ha_close < ha_open
    sert_yukselis = ha_close > ha_high.shift(1)
    sert_dusus = ha_close < ha_low.shift(1)

    prev_ha_down = ha_is_down.shift(1).fillna(False)
    prev_ha_up = ha_is_up.shift(1).fillna(False)

    signal_al = ha_is_up & (prev_ha_down | sert_yukselis) & (mutlak_degisim >= ROKET_HASSASIYET)
    signal_sat = ha_is_down & (prev_ha_up | sert_dusus) & (mutlak_degisim >= ROKET_HASSASIYET)

    atr_val = _atr(df, ROKET_ATR_PERIOD)

    # ROKET AL/SAT = Quantum Golden COK GUCLU donusu + Major Level kesisimi (2 sart)
    roket_al = signal_al & crossed_up
    roket_sat = signal_sat & crossed_down

    i = -1  # CANLI mum

    if bool(roket_al.iloc[i]):
        return {
            "direction": "AL",
            "price": round(float(close.iloc[i]), 4),
            "major_level": round(float(major_level.iloc[i]), 4) if not pd.isna(major_level.iloc[i]) else None,
            "olasilik": round(float(olasilik_son[i]), 0) if not pd.isna(olasilik_son[i]) else None,
            "target": round(float(close.iloc[i] + atr_val.iloc[i] * 2), 4) if not pd.isna(atr_val.iloc[i]) else None,
        }

    if bool(roket_sat.iloc[i]):
        return {
            "direction": "SAT",
            "price": round(float(close.iloc[i]), 4),
            "major_level": round(float(major_level.iloc[i]), 4) if not pd.isna(major_level.iloc[i]) else None,
            "olasilik": round(float(olasilik_son[i]), 0) if not pd.isna(olasilik_son[i]) else None,
            "target": round(float(close.iloc[i] - atr_val.iloc[i] * 2), 4) if not pd.isna(atr_val.iloc[i]) else None,
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
    emoji = "ROKET AL" if sig["direction"] == "AL" else "ROKET SAT"
    coin = symbol.replace("USDT", "/USDT")
    sep = "=" * 16

    lines = [
        f"{emoji}",
        sep,
        f"Coin: {coin}",
        f"Fiyat: {sig['price']}",
        f"Major Level: {sig['major_level']}",
        f"Hedef (ATRx2): {sig['target']}",
        f"Olasilik: %{sig['olasilik']}",
        sep,
        f"{datetime.now().strftime('%H:%M:%S %d/%m/%Y')}",
    ]

    return "\n".join(lines)


def run_scan():
    symbols = get_symbols()
    log.info(f"ROKET AL/SAT TARAMA basladi Coin:{len(symbols)}")
    found = 0

    for idx, symbol in enumerate(symbols):
        try:
            df_roket = get_klines(symbol, ROKET_TIMEFRAME, limit=ROKET_KLINES_LIMIT)
            sig = check_roket_live(df_roket)

            if sig and should_send(symbol, sig["direction"]):
                if send_telegram(format_message(symbol, sig)):
                    found += 1
                    log.info(f"ROKET {symbol} {sig['direction']} Fiyat:{sig['price']} Olasilik:%{sig['olasilik']}")

            time.sleep(0.2)

        except Exception as e:
            log.error(f"{symbol} hata: {e}")
            continue

        if (idx + 1) % 50 == 0:
            log.info(f"[{idx+1}/{len(symbols)}] tarandi {found} sinyal")

    log.info(f"Tarama tamamlandi {found} sinyal gonderildi")


def main():
    log.info("ROKET AL/SAT BOT baslatildi")

    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log.error("TELEGRAM_TOKEN ve TELEGRAM_CHAT_ID eksik!")
        return

    send_telegram(
        f"ROKET AL/SAT BOT BASLADI\n"
        f"Roket TF: {ROKET_TIMEFRAME} (CANLI)\n"
        f"Major Level SMA: {ROKET_MAJOR_LEN}\n"
        f"Tarama Araligi: {SCAN_INTERVAL}sn | Coin: {MAX_COINS}\n"
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
