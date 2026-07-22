# -*- coding: utf-8 -*-
"""
SMA100 + VOLUME SPIKE + EMA CROSSOVER + RSI SCANNER BOT
- Binance Futures USDT ciftlerini PARALEL tarar
- 1H grafikte coklu onayli AL sinyali uretir
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
EMA_FAST = int(os.getenv("EMA_FAST", "5"))
EMA_SLOW = int(os.getenv("EMA_SLOW", "20"))
RSI_LEN = int(os.getenv("RSI_LEN", "14"))
RSI_LEVEL = float(os.getenv("RSI_LEVEL", "50.0"))

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


def calc_rsi(series, length=14):
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1/length, min_periods=length, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1/length, min_periods=length, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def calc_ema(series, length):
    return series.ewm(span=length, adjust=False).mean()


def check_signal(symbol):
    """Tek coini kontrol et - ThreadPool icin"""
    try:
        df = get_klines(symbol, TIMEFRAME, limit=KLINES_LIMIT)
        if df is None or len(df) < max(MAJOR_LINE_LEN, EMA_SLOW, RSI_LEN) + 5:
            return None, {"symbol": symbol, "status": "no_data"}

        close = df["close"]
        high = df["high"]
        low = df["low"]
        volume = df["volume"]

        # 1. SMA100 (Major Level)
        sma100 = close.rolling(MAJOR_LINE_LEN).mean().iloc[-1]
        if pd.isna(sma100):
            return None, {"symbol": symbol, "status": "no_sma"}

        close_now = float(close.iloc[-1])
        high_now = float(high.iloc[-1])
        low_now = float(low.iloc[-1])
        vol_now = float(volume.iloc[-1])

        sma100_val = float(sma100)

        # Fiyat SMA100'e %2 yakın mı?
        sma_upper = sma100_val * (1 + MIN_DISTANCE_PCT / 100)
        sma_lower = sma100_val * (1 - MIN_DISTANCE_PCT / 100)
        near_sma = (low_now <= sma_upper and high_now >= sma_lower)

        if not near_sma:
            return None, {"symbol": symbol, "status": "far_from_sma", "dist_pct": abs(close_now - sma100_val) / sma100_val * 100}

        # 2. Hacim >= 5x
        vol_sma20 = volume.rolling(20).mean().iloc[-1]
        vol_ratio = vol_now / vol_sma20 if vol_sma20 > 0 else 1.0

        if vol_ratio < VOL_MULTIPLIER:
            return None, {"symbol": symbol, "status": "low_volume", "vol_ratio": vol_ratio}

        # 3. EMA Crossover (EMA5 > EMA20)
        ema_fast = calc_ema(close, EMA_FAST)
        ema_slow = calc_ema(close, EMA_SLOW)

        ema_fast_now = float(ema_fast.iloc[-1])
        ema_slow_now = float(ema_slow.iloc[-1])
        ema_fast_prev = float(ema_fast.iloc[-2])
        ema_slow_prev = float(ema_slow.iloc[-2])

        ema_cross = ema_fast_prev <= ema_slow_prev and ema_fast_now > ema_slow_now

        if not ema_cross:
            return None, {"symbol": symbol, "status": "no_ema_cross"}

        # 4. RSI > 50
        rsi_series = calc_rsi(close, RSI_LEN)
        rsi_now = float(rsi_series.iloc[-1]) if not pd.isna(rsi_series.iloc[-1]) else 50.0

        if rsi_now <= RSI_LEVEL:
            return None, {"symbol": symbol, "status": "rsi_low", "rsi": rsi_now}

        # Cooldown kontrolu
        key = f"{symbol}_AL"
        now = time.time()
        if now - last_signal.get(key, 0) < SIGNAL_COOLDOWN:
            return None, {"symbol": symbol, "status": "cooldown"}

        last_signal[key] = now

        # Sinyal olustur
        details = {
            "price": close_now,
            "sma100": sma100_val,
            "dist_pct": (close_now - sma100_val) / sma100_val * 100,
            "vol_ratio": vol_ratio,
            "ema_fast": ema_fast_now,
            "ema_slow": ema_slow_now,
            "rsi": rsi_now,
            "high": high_now,
            "low": low_now
        }

        return details, {"symbol": symbol, "status": "signal", "vol_ratio": vol_ratio, "rsi": rsi_now}

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
    sep = "═" * 24

    lines = [
        f"🚀 CONFLUENCE AL SINYALI",
        sep,
        f"💱 Coin: {coin}",
        f"💰 Fiyat: {details['price']:.6f}",
        f"📍 SMA{MAJOR_LINE_LEN}: {details['sma100']:.6f}",
        f"📊 SMA Mesafe: %{details['dist_pct']:.2f}",
        f"📈 Hacim: {details['vol_ratio']:.2f}x (min {VOL_MULTIPLIER}x)",
        f"📊 EMA{EMA_FAST}/{EMA_SLOW}: {details['ema_fast']:.2f} > {details['ema_slow']:.2f}",
        f"📈 RSI: {details['rsi']:.1f} (>{RSI_LEVEL})",
        sep,
        f"⏰ {datetime.now().strftime('%H:%M:%S %d/%m/%Y')}",
    ]

    return "\n".join(lines)


def run_scan_parallel():
    symbols = get_symbols()
    total = len(symbols)
    log.info(f"CONFLUENCE TARAMA | Coin: {total} | Workers: {MAX_WORKERS} | TF: {TIMEFRAME}")

    stats = {
        "total": total,
        "signal": 0,
        "far": 0,
        "low_vol": 0,
        "no_ema": 0,
        "rsi_low": 0,
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
                elif info["status"] == "no_ema_cross":
                    stats["no_ema"] += 1
                elif info["status"] == "rsi_low":
                    stats["rsi_low"] += 1
                elif info["status"] == "cooldown":
                    stats["cooldown"] += 1
                elif info["status"] == "error":
                    stats["error"] += 1

            except Exception as e:
                log.error(f"Future hata: {e}")
                stats["error"] += 1

            if completed % 100 == 0 or completed == total:
                log.info(f"[{completed}/{total}] | Sinyal: {stats['signal']} | Uzak: {stats['far']} | DusukVol: {stats['low_vol']} | NoEMA: {stats['no_ema']} | DusukRSI: {stats['rsi_low']} | Hata: {stats['error']}")

    for symbol, details in signals_found:
        try:
            msg = format_message(symbol, details)
            if send_telegram(msg):
                log.info(f"SINYAL {symbol} Fiyat:{details['price']:.6f} Vol:{details['vol_ratio']:.2f}x RSI:{details['rsi']:.1f}")
            else:
                log.error(f"Telegram gonderilemedi: {symbol}")
        except Exception as e:
            log.error(f"Sinyal gonderim hatasi {symbol}: {e}")

    log.info(f"Tarama tamamlandi | {stats['signal']} sinyal | Stats: {stats}")
    return stats['signal']


def main():
    log.info("CONFLUENCE SCANNER BOT baslatildi")

    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log.error("TELEGRAM_TOKEN ve TELEGRAM_CHAT_ID eksik!")
        return

    send_telegram(
        f"🚀 CONFLUENCE SCANNER BOT BASLADI\n"
        f"═" * 30 + "\n"
        f"💱 TF: {TIMEFRAME}\n"
        f"📊 SMA{MAJOR_LINE_LEN} + Vol>={VOL_MULTIPLIER}x\n"
        f"📈 EMA{EMA_FAST}/{EMA_SLOW} Crossover\n"
        f"📈 RSI > {RSI_LEVEL}\n"
        f"📍 SMA Mesafe: %{MIN_DISTANCE_PCT}\n"
        f"⏰ Cooldown: {SIGNAL_COOLDOWN}sn\n"
        f"⚡ Workers: {MAX_WORKERS}\n"
        f"⏰ Interval: {SCAN_INTERVAL}sn | Coins: {MAX_COINS}\n"
        f"\n"
        f"🟢 AL: SMA yakin + Vol patlamasi + EMA kesisim + RSI > 50"
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
