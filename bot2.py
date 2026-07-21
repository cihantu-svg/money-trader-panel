# -*- coding: utf-8 -*-
"""
MAJOR LEVEL VOLUME SPIKE SCANNER BOT - PARALEL TARAMA
- Binance Futures USDT ciftlerini PARALEL tarar (ThreadPool)
- 1H grafikte SMA100 (Major Level / Turuncu Cizgi) yakininda
- Hacim >= 5x ortalama olan coinleri bulur
- Sadece AL sinyali uretir
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
# AYARLAR (.env'den okunur)
# ═══════════════════════════════════════════════════════════════
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
SCAN_INTERVAL = int(os.getenv("SCAN_INTERVAL", "300"))
MAX_COINS = int(os.getenv("MAX_COINS", "600"))
SIGNAL_COOLDOWN = int(os.getenv("SIGNAL_COOLDOWN", "7200"))
TIMEFRAME = os.getenv("TIMEFRAME", "1h")
KLINES_LIMIT = int(os.getenv("KLINES_LIMIT", "200"))

# PARALEL TARAMA
MAX_WORKERS = int(os.getenv("MAX_WORKERS", "20"))
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "8"))

# STRATEJI AYARLARI
MAJOR_LINE_LEN = int(os.getenv("MAJOR_LINE_LEN", "100"))
VOL_MULTIPLIER = float(os.getenv("VOL_MULTIPLIER", "5.0"))
MIN_DISTANCE_PCT = float(os.getenv("MIN_DISTANCE_PCT", "2.0"))

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


def check_signal(symbol):
    """Tek coini kontrol et - ThreadPool icin"""
    try:
        df = get_klines(symbol, TIMEFRAME, limit=KLINES_LIMIT)
        if df is None or len(df) < MAJOR_LINE_LEN + 5:
            return None, {"symbol": symbol, "status": "no_data"}

        # SMA100 hesapla (Major Level / Turuncu Cizgi)
        sma100 = df["close"].rolling(MAJOR_LINE_LEN).mean().iloc[-1]
        if pd.isna(sma100):
            return None, {"symbol": symbol, "status": "no_sma"}

        # Son mum verileri
        close = float(df["close"].iloc[-1])
        high = float(df["high"].iloc[-1])
        low = float(df["low"].iloc[-1])
        volume = float(df["volume"].iloc[-1])

        # Fiyat SMA100'e %2 yakınında mı?
        sma_upper = sma100 * (1 + MIN_DISTANCE_PCT / 100)
        sma_lower = sma100 * (1 - MIN_DISTANCE_PCT / 100)

        near_sma = (low <= sma_upper and high >= sma_lower)

        if not near_sma:
            return None, {"symbol": symbol, "status": "far_from_sma", "dist_pct": abs(close - sma100) / sma100 * 100}

        # Hacim >= 5x ortalama?
        vol_sma20 = df["volume"].rolling(20).mean().iloc[-1]
        vol_ratio = volume / vol_sma20 if vol_sma20 > 0 else 1.0

        if vol_ratio < VOL_MULTIPLIER:
            return None, {"symbol": symbol, "status": "low_volume", "vol_ratio": vol_ratio}

        # Cooldown kontrolu
        key = f"{symbol}_AL"
        now = time.time()
        if now - last_signal.get(key, 0) < SIGNAL_COOLDOWN:
            return None, {"symbol": symbol, "status": "cooldown"}

        last_signal[key] = now

        # Sinyal olustur
        details = {
            "price": close,
            "sma100": float(sma100),
            "dist_pct": (close - sma100) / sma100 * 100,
            "vol_ratio": vol_ratio,
            "high": high,
            "low": low
        }

        return details, {"symbol": symbol, "status": "signal", "vol_ratio": vol_ratio}

    except Exception as e:
        return None, {"symbol": symbol, "status": "error", "error": str(e)}


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


def format_message(symbol, details):
    coin = symbol.replace("USDT", "/USDT")
    sep = "═" * 22

    lines = [
        f"🟢 SMA100 VOLUME SPIKE AL",
        sep,
        f"💱 Coin: {coin}",
        f"💰 Fiyat: {details['price']:.6f}",
        f"📍 SMA100: {details['sma100']:.6f}",
        f"📊 SMA Mesafe: %{details['dist_pct']:.2f}",
        f"📈 Hacim: {details['vol_ratio']:.2f}x (min {VOL_MULTIPLIER}x)",
        sep,
        f"⏰ {datetime.now().strftime('%H:%M:%S %d/%m/%Y')}",
    ]

    return "\n".join(lines)


def run_scan_parallel():
    symbols = get_symbols()
    total = len(symbols)
    log.info(f"SMA100 VOLUME SPIKE TARAMA | Coin: {total} | Workers: {MAX_WORKERS} | TF: {TIMEFRAME} | VolMin: {VOL_MULTIPLIER}x")

    stats = {
        "total": total,
        "signal": 0,
        "far": 0,
        "low_vol": 0,
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
                    signals_found.append((info["symbol"], signal))
                elif info["status"] == "far_from_sma":
                    stats["far"] += 1
                elif info["status"] == "low_volume":
                    stats["low_vol"] += 1
                elif info["status"] == "cooldown":
                    stats["cooldown"] += 1
                elif info["status"] == "error":
                    stats["error"] += 1

            except Exception as e:
                log.error(f"Future hata: {e}")
                stats["error"] += 1

            if completed % 100 == 0 or completed == total:
                log.info(f"[{completed}/{total}] | Sinyal: {stats['signal']} | Uzak: {stats['far']} | DusukVol: {stats['low_vol']} | Hata: {stats['error']}")

    # Sinyalleri gonder
    for symbol, details in signals_found:
        try:
            msg = format_message(symbol, details)
            if send_telegram(msg):
                log.info(f"SINYAL {symbol} Fiyat:{details['price']:.6f} Vol:{details['vol_ratio']:.2f}x")
            else:
                log.error(f"Telegram gonderilemedi: {symbol}")
        except Exception as e:
            log.error(f"Sinyal gonderim hatasi {symbol}: {e}")

    log.info(f"Tarama tamamlandi | {stats['signal']} sinyal | Stats: {stats}")
    return stats['signal']


def main():
    log.info("SMA100 VOLUME SPIKE BOT baslatildi")

    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log.error("TELEGRAM_TOKEN ve TELEGRAM_CHAT_ID eksik!")
        return

    send_telegram(
        f"🎯 SMA100 VOLUME SPIKE BOT BASLADI\n"
        f"═" * 28 + "\n"
        f"💱 TF: {TIMEFRAME}\n"
        f"📊 SMA{MAJOR_LINE_LEN} (Major Level)\n"
        f"📈 Min Hacim: {VOL_MULTIPLIER}x\n"
        f"📍 SMA Mesafe: %{MIN_DISTANCE_PCT}\n"
        f"⏰ Cooldown: {SIGNAL_COOLDOWN}sn\n"
        f"⚡ Workers: {MAX_WORKERS} (PARALEL)\n"
        f"⏰ Interval: {SCAN_INTERVAL}sn | Coins: {MAX_COINS}\n"
        f"\n"
        f"🟢 AL: Fiyat SMA{MAJOR_LINE_LEN}'e %2 yakın + Hacim >= {VOL_MULTIPLIER}x"
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
