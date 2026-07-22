# -*- coding: utf-8 -*-
"""
SMA100 PEAK BREAKOUT SCANNER BOT
- 15m grafikte SMA100 kiran ve retest eden coinleri bulur
- Son kiran tepeyi/dibi hacimli mumla tekrar kirinca sinyal verir
"""
import os, time, logging
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
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
TIMEFRAME = os.getenv("TIMEFRAME", "15m")
KLINES_LIMIT = int(os.getenv("KLINES_LIMIT", "200"))

MAX_WORKERS = int(os.getenv("MAX_WORKERS", "20"))
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "8"))

MAJOR_LINE_LEN = int(os.getenv("MAJOR_LINE_LEN", "100"))
VOL_MULTIPLIER = float(os.getenv("VOL_MULTIPLIER", "5.0"))
BREAK_LOOKBACK = int(os.getenv("BREAK_LOOKBACK", "10"))
PEAK_BREAK_PCT = float(os.getenv("PEAK_BREAK_PCT", "0.5"))

BINANCE_BASE = "https://fapi.binance.com"
last_signal = {}

# ═══════════════════════════════════════════════════════════════
# FONKSIYONLAR
# ═══════════════════════════════════════════════════════════════

def get_symbols():
    try:
        session = requests.Session()
        r = session.get(f"{BINANCE_BASE}/fapi/v1/exchangeInfo", timeout=10)
        data = r.json()
        syms = [s["symbol"] for s in data["symbols"]
                if s["symbol"].endswith("USDT") and s["status"] == "TRADING"]
        return syms[:MAX_COINS]
    except Exception as e:
        log.error(f"get_symbols hata: {e}")
        return []


def get_klines(symbol, interval, limit=200):
    try:
        session = requests.Session()
        r = session.get(
            f"{BINANCE_BASE}/fapi/v1/klines",
            params={"symbol": symbol, "interval": interval, "limit": limit},
            timeout=REQUEST_TIMEOUT
        )
        raw = r.json()
        df = pd.DataFrame(raw, columns=[
            "open_time","open","high","low","close","volume","close_time",
            "qav","trades","tbv","tqv","ignore"])
        for c in ["open","high","low","close","volume"]:
            df[c] = df[c].astype(float)
        return df
    except Exception:
        return None


def find_peak_break(df):
    """SMA100 kiran ve retest eden coinleri bul"""
    if df is None or len(df) < MAJOR_LINE_LEN + BREAK_LOOKBACK + 5:
        return None, None

    close = df["close"].values
    high = df["high"].values
    low = df["low"].values
    volume = df["volume"].values

    # SMA100 hesapla
    sma100 = pd.Series(close).rolling(MAJOR_LINE_LEN).mean().values

    if np.isnan(sma100[-1]):
        return None, None

    # Son BREAK_LOOKBACK mumda SMA100 kiran mumu bul
    break_idx = None
    break_direction = None

    for i in range(-BREAK_LOOKBACK - 1, -1):
        if np.isnan(sma100[i]) or np.isnan(sma100[i-1]):
            continue

        # Yukari kirilim
        if close[i-1] <= sma100[i-1] and close[i] > sma100[i]:
            break_idx = i
            break_direction = "AL"
            break

        # Asagi kirilim
        if close[i-1] >= sma100[i-1] and close[i] < sma100[i]:
            break_idx = i
            break_direction = "SAT"
            break

    if break_idx is None or break_direction is None:
        return None, None

    # Kirilimdan sonraki peak/dip bul
    if break_direction == "AL":
        peak = np.max(high[break_idx:])

        # Simdi fiyat peak'i yukari kiriyor mu?
        current_high = high[-1]
        current_close = close[-1]
        peak_threshold = peak * (1 + PEAK_BREAK_PCT / 100)

        if current_high < peak_threshold:
            return None, None

        # Hacim kontrolu
        vol_sma20 = np.mean(volume[-21:-1])
        vol_ratio = volume[-1] / vol_sma20 if vol_sma20 > 0 else 1.0

        if vol_ratio < VOL_MULTIPLIER:
            return None, None

        # Fiyat SMA100 uzerinde mi?
        if close[-1] <= sma100[-1]:
            return None, None

        return "AL", {
            "price": current_close,
            "sma100": float(sma100[-1]),
            "peak": float(peak),
            "vol_ratio": float(vol_ratio),
            "break_idx": break_idx
        }

    else:  # SAT
        dip = np.min(low[break_idx:])

        current_low = low[-1]
        current_close = close[-1]
        dip_threshold = dip * (1 - PEAK_BREAK_PCT / 100)

        if current_low > dip_threshold:
            return None, None

        vol_sma20 = np.mean(volume[-21:-1])
        vol_ratio = volume[-1] / vol_sma20 if vol_sma20 > 0 else 1.0

        if vol_ratio < VOL_MULTIPLIER:
            return None, None

        if close[-1] >= sma100[-1]:
            return None, None

        return "SAT", {
            "price": current_close,
            "sma100": float(sma100[-1]),
            "dip": float(dip),
            "vol_ratio": float(vol_ratio),
            "break_idx": break_idx
        }


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


def format_message(symbol, direction, details):
    emoji = "🟢 PEAK BREAKOUT AL" if direction == "AL" else "🔴 DIP BREAKOUT SAT"
    coin = symbol.replace("USDT", "/USDT")
    sep = "═" * 24

    if direction == "AL":
        extra = f"📈 Peak: {details['peak']:.6f}"
    else:
        extra = f"📉 Dip: {details['dip']:.6f}"

    lines = [
        f"{emoji}",
        sep,
        f"💱 Coin: {coin}",
        f"💰 Fiyat: {details['price']:.6f}",
        f"📍 SMA100: {details['sma100']:.6f}",
        extra,
        f"📊 Hacim: {details['vol_ratio']:.2f}x (min {VOL_MULTIPLIER}x)",
        sep,
        f"⏰ {datetime.now().strftime('%H:%M:%S %d/%m/%Y')}",
    ]

    return "\n".join(lines)


def check_signal(symbol):
    try:
        df = get_klines(symbol, TIMEFRAME, limit=KLINES_LIMIT)
        direction, details = find_peak_break(df)

        if direction is None:
            return None, {"symbol": symbol, "status": "no_signal"}

        if not should_send(symbol, direction):
            return None, {"symbol": symbol, "status": "cooldown"}

        return details, {"symbol": symbol, "status": "signal", "direction": direction}

    except Exception as e:
        return None, {"symbol": symbol, "status": "error", "error": str(e)}


def run_scan_parallel():
    symbols = get_symbols()
    total = len(symbols)
    log.info(f"PEAK BREAKOUT TARAMA | Coin: {total} | Workers: {MAX_WORKERS} | TF: {TIMEFRAME}")

    stats = {
        "total": total,
        "signal": 0,
        "no_signal": 0,
        "cooldown": 0,
        "error": 0
    }

    signals_found = []

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        future_to_symbol = {
            executor.submit(check_signal, sym): sym 
            for sym in symbols
        }

        completed = 0
        for future in as_completed(future_to_symbol):
            completed += 1

            try:
                signal, info = future.result()

                if info["status"] == "signal":
                    stats["signal"] += 1
                    signals_found.append((info["symbol"], info["direction"], signal))
                elif info["status"] == "no_signal":
                    stats["no_signal"] += 1
                elif info["status"] == "cooldown":
                    stats["cooldown"] += 1
                elif info["status"] == "error":
                    stats["error"] += 1

            except Exception as e:
                log.error(f"Future hata: {e}")
                stats["error"] += 1

            if completed % 100 == 0 or completed == total:
                log.info(f"[{completed}/{total}] | Sinyal: {stats['signal']} | NoSignal: {stats['no_signal']} | Hata: {stats['error']}")

    for symbol, direction, details in signals_found:
        try:
            msg = format_message(symbol, direction, details)
            if send_telegram(msg):
                log.info(f"SINYAL {symbol} {direction} Fiyat:{details['price']:.6f} Vol:{details['vol_ratio']:.2f}x")
            else:
                log.error(f"Telegram gonderilemedi: {symbol}")
        except Exception as e:
            log.error(f"Sinyal gonderim hatasi {symbol}: {e}")

    log.info(f"Tarama tamamlandi | {stats['signal']} sinyal | Stats: {stats}")
    return stats['signal']


def main():
    log.info("PEAK BREAKOUT SCANNER BOT baslatildi")

    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log.error("TELEGRAM_TOKEN ve TELEGRAM_CHAT_ID eksik!")
        return

    send_telegram(
        f"🚀 PEAK BREAKOUT SCANNER BASLADI\n"
        f"═" * 30 + "\n"
        f"💱 TF: {TIMEFRAME}\n"
        f"📊 SMA{MAJOR_LINE_LEN}\n"
        f"👆 Son {BREAK_LOOKBACK} mumda kirma\n"
        f"📈 Hacim: >={VOL_MULTIPLIER}x\n"
        f"⏰ Cooldown: {SIGNAL_COOLDOWN}sn\n"
        f"⚡ Workers: {MAX_WORKERS}\n"
        f"\n"
        f"🟢 AL: SMA100 yukari kir -> retest -> peak kir + hacim\n"
        f"🔴 SAT: SMA100 asagi kir -> retest -> dip kir + hacim"
    )

    while True:
        try:
            run_scan_parallel()
        except Exception as e:
            log.error(f"run_scan genel hata: {e}")

        log.info(f"{SCAN_INTERVAL}sn bekleniyor...")
        time.sleep(SCAN_INTERVAL)


if __name__ == "__main__":
    main()
