# -*- coding: utf-8 -*-
"""
MAJOR ZONE BREAKOUT SCANNER BOT - PARALEL TARAMA
- Binance Futures USDT ciftlerini PARALEL tarar (ThreadPool)
- 500 mum geriye bakarak major bolgeyi bulur (histogram yontemi)
- Temas sayisi >= 70 olan bolgeleri "GUCCLU MAJOR" olarak isaretler
- Bu bolge min %3 mum boyu ile kirildiginda AL/SAT sinyali uretir
- Hacim ve RSI onayi ile false signal azaltir
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
# AYARLAR (Mevcut sistemle uyumlu - .env'den okunur)
# ═══════════════════════════════════════════════════════════════
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
SCAN_INTERVAL = int(os.getenv("SCAN_INTERVAL", "300"))
MAX_COINS = int(os.getenv("MAX_COINS", "600"))
SIGNAL_COOLDOWN = int(os.getenv("SIGNAL_COOLDOWN", "3600"))
TIMEFRAME = os.getenv("TIMEFRAME", "5m")
KLINES_LIMIT = int(os.getenv("KLINES_LIMIT", "500"))

# PARALEL TARAMA AYARLARI
MAX_WORKERS = int(os.getenv("MAX_WORKERS", "20"))
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "8"))

# MAJOR ZONE STRATEJI AYARLARI
MIN_TOUCHES = int(os.getenv("MIN_TOUCHES", "70"))
MAJOR_BINS = int(os.getenv("MAJOR_BINS", "10"))
ZONE_LOOKBACK = int(os.getenv("ZONE_LOOKBACK", "500"))
MIN_BREAK_PCT = float(os.getenv("MIN_BREAK_PCT", "3.0"))
VOL_MULTIPLIER = float(os.getenv("VOL_MULTIPLIER", "1.2"))
RSI_LEN = int(os.getenv("RSI_LEN", "14"))
RSI_LEVEL = float(os.getenv("RSI_LEVEL", "50.0"))

BINANCE_BASE = "https://fapi.binance.com"
last_signal = {}

# ═══════════════════════════════════════════════════════════════
# FONKSIYONLAR
# ═══════════════════════════════════════════════════════════════

def get_symbols():
    """Binance Futures USDT ciftlerini cek"""
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


def get_klines(symbol, interval, limit=500):
    """Binance'den kline verisi cek - her thread kendi session'ini kullanir"""
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
    """RSI hesapla"""
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1/length, min_periods=length, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1/length, min_periods=length, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def find_major_zone(df, bins=10, lookback=500):
    """Histogram yontemi ile major bolge bulur"""
    if df is None or len(df) < lookback:
        return None, 0

    df_hist = df.iloc[-lookback:].copy()
    highs = df_hist["high"].values
    lows = df_hist["low"].values
    closes = df_hist["close"].values

    auto_highest = np.max(highs)
    auto_lowest = np.min(lows)
    price_range = auto_highest - auto_lowest

    if price_range <= 0 or bins <= 0:
        return None, 0

    step = price_range / bins

    bin_counts = np.zeros(bins, dtype=int)
    for p in closes:
        idx = min(bins - 1, max(0, int((p - auto_lowest) / step)))
        bin_counts[idx] += 1

    max_bin = int(np.argmax(bin_counts))

    major_top = auto_lowest + step * (max_bin + 1)
    major_btm = auto_lowest + step * max_bin

    touches = 0
    for i in range(len(df_hist)):
        if (highs[i] >= major_btm and highs[i] <= major_top) or \
           (lows[i] >= major_btm and lows[i] <= major_top):
            touches += 1

    return {
        "top": float(major_top),
        "btm": float(major_btm),
        "touches": touches,
        "highest": float(auto_highest),
        "lowest": float(auto_lowest),
        "center": float((major_top + major_btm) / 2)
    }, touches


def check_major_break(df, major_zone, min_break_pct=3.0, vol_mult=1.2, rsi_len=14, rsi_level=50):
    """Major bolgenin kirilip kirilmadigini kontrol et"""
    if df is None or len(df) < 2 or major_zone is None:
        return False, None, {}

    if major_zone["touches"] < MIN_TOUCHES:
        return False, None, {}

    high = float(df["high"].iloc[-1])
    low = float(df["low"].iloc[-1])
    open_ = float(df["open"].iloc[-1])
    close = float(df["close"].iloc[-1])
    volume = float(df["volume"].iloc[-1])

    zone_top = major_zone["top"]
    zone_btm = major_zone["btm"]
    zone_height = zone_top - zone_btm

    if zone_height <= 0:
        return False, None, {}

    candle_body = abs(close - open_)
    candle_range = high - low
    body_pct = (candle_body / zone_height) * 100
    range_pct = (candle_range / zone_height) * 100

    vol_sma20 = df["volume"].rolling(20).mean().iloc[-1]
    vol_ratio = volume / vol_sma20 if vol_sma20 > 0 else 1.0
    vol_ok = vol_ratio >= vol_mult

    rsi_series = calc_rsi(df["close"], rsi_len)
    rsi_now = float(rsi_series.iloc[-1]) if not pd.isna(rsi_series.iloc[-1]) else 50.0

    break_up = high > zone_top and (body_pct >= min_break_pct or range_pct >= min_break_pct)
    rsi_up_ok = rsi_now > rsi_level

    break_down = low < zone_btm and (body_pct >= min_break_pct or range_pct >= min_break_pct)
    rsi_down_ok = rsi_now < rsi_level

    details = {
        "price": close,
        "high": high,
        "low": low,
        "zone_top": zone_top,
        "zone_btm": zone_btm,
        "zone_height": zone_height,
        "zone_height_pct": (zone_height / zone_btm) * 100,
        "touches": major_zone["touches"],
        "candle_body_pct": body_pct,
        "candle_range_pct": range_pct,
        "vol_ratio": vol_ratio,
        "vol_ok": vol_ok,
        "rsi": rsi_now,
        "center": major_zone["center"]
    }

    if break_up and vol_ok and rsi_up_ok:
        return True, "AL", details

    if break_down and vol_ok and rsi_down_ok:
        return True, "SAT", details

    return False, None, details


def should_send(symbol, direction):
    """Cooldown kontrolu"""
    key = f"{symbol}_{direction}"
    now = time.time()
    if now - last_signal.get(key, 0) < SIGNAL_COOLDOWN:
        return False
    last_signal[key] = now
    return True


def send_telegram(text):
    """Telegram mesaji gonder"""
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
    """Telegram mesaj formati"""
    emoji = "🟢 MAJOR ZONE BREAKOUT AL" if direction == "AL" else "🔴 MAJOR ZONE BREAKOUT SAT"
    coin = symbol.replace("USDT", "/USDT")
    sep = "═" * 22

    lines = [
        f"{emoji}",
        sep,
        f"💱 Coin: {coin}",
        f"💰 Fiyat: {details['price']:.6f}",
        f"📍 Major Zone: {details['zone_btm']:.6f} - {details['zone_top']:.6f}",
        f"📊 Zone Yüksekliği: %{details['zone_height_pct']:.2f}",
        f"👆 Temas Sayısı: {details['touches']} kez",
        f"📈 Break Mum Boyu: %{details['candle_body_pct']:.1f}",
        f"📊 Hacim: {details['vol_ratio']:.2f}x (min {VOL_MULTIPLIER}x)",
        f"📈 RSI: {details['rsi']:.1f}",
        sep,
        f"⏰ {datetime.now().strftime('%H:%M:%S %d/%m/%Y')}",
    ]

    return "\n".join(lines)


def analyze_single_coin(symbol):
    """Tek bir coini analiz et - ThreadPool icin"""
    try:
        df = get_klines(symbol, TIMEFRAME, limit=KLINES_LIMIT)
        if df is None or len(df) < ZONE_LOOKBACK:
            return None, {"symbol": symbol, "status": "no_data"}

        major_zone, touches = find_major_zone(df, MAJOR_BINS, ZONE_LOOKBACK)

        if major_zone is None:
            return None, {"symbol": symbol, "status": "no_major"}

        if touches < MIN_TOUCHES:
            return None, {"symbol": symbol, "status": "weak_major", "touches": touches}

        is_break, direction, details = check_major_break(
            df, major_zone, MIN_BREAK_PCT, VOL_MULTIPLIER, RSI_LEN, RSI_LEVEL
        )

        if not is_break or direction is None:
            return None, {"symbol": symbol, "status": "no_break", "touches": touches}

        if not should_send(symbol, direction):
            return None, {"symbol": symbol, "status": "cooldown", "touches": touches}

        msg = format_message(symbol, direction, details)

        signal = {
            "direction": direction,
            "symbol": symbol,
            "price": details["price"],
            "message": msg,
            "details": details
        }

        return signal, {"symbol": symbol, "status": "signal", "touches": touches, "direction": direction}

    except Exception as e:
        return None, {"symbol": symbol, "status": "error", "error": str(e)}


def run_scan_parallel():
    """PARALEL TARAMA - ThreadPool ile"""
    symbols = get_symbols()
    total = len(symbols)
    log.info(f"MAJOR ZONE PARALEL TARAMA | Coin: {total} | Workers: {MAX_WORKERS} | TF: {TIMEFRAME} | MinTouches: {MIN_TOUCHES}")

    stats = {
        "total": total,
        "processed": 0,
        "has_major": 0,
        "major_70plus": 0,
        "break_signals": 0,
        "signals_sent": 0,
        "errors": 0,
        "no_data": 0
    }

    signals_found = []

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        future_to_symbol = {
            executor.submit(analyze_single_coin, sym): sym 
            for sym in symbols
        }

        completed = 0
        for future in as_completed(future_to_symbol):
            completed += 1
            symbol = future_to_symbol[future]

            try:
                signal, info = future.result()
                stats["processed"] += 1

                if info["status"] == "signal":
                    stats["break_signals"] += 1
                    stats["major_70plus"] += 1
                    signals_found.append(signal)
                elif info["status"] == "weak_major":
                    stats["has_major"] += 1
                elif info["status"] == "no_break":
                    stats["major_70plus"] += 1
                elif info["status"] == "no_data":
                    stats["no_data"] += 1
                elif info["status"] == "error":
                    stats["errors"] += 1

            except Exception as e:
                log.error(f"{symbol} future hata: {e}")
                stats["errors"] += 1

            if completed % 100 == 0 or completed == total:
                log.info(f"[{completed}/{total}] | Sinyal: {stats['break_signals']} | 70+Major: {stats['major_70plus']} | Hata: {stats['errors']}")

    for sig in signals_found:
        try:
            if send_telegram(sig["message"]):
                stats["signals_sent"] += 1
                log.info(f"SINYAL {sig['symbol']} {sig['direction']} Fiyat:{sig['price']:.6f} Temas:{sig['details']['touches']}")
            else:
                log.error(f"Telegram gonderilemedi: {sig['symbol']}")
        except Exception as e:
            log.error(f"Sinyal gonderim hatasi {sig['symbol']}: {e}")

    log.info(f"Tarama tamamlandi | {stats['signals_sent']} sinyal | Stats: {stats}")
    return stats['signals_sent'], stats


def main():
    log.info("MAJOR ZONE BREAKOUT BOT baslatildi (PARALEL)")

    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log.error("TELEGRAM_TOKEN ve TELEGRAM_CHAT_ID eksik!")
        return

    send_telegram(
        f"🎯 MAJOR ZONE BREAKOUT BOT BASLADI\n"
        f"═" * 28 + "\n"
        f"💱 TF: {TIMEFRAME} | Lookback: {ZONE_LOOKBACK}\n"
        f"👆 Min Temas: {MIN_TOUCHES} | Min Break: %{MIN_BREAK_PCT}\n"
        f"📊 Hacim: {VOL_MULTIPLIER}x | RSI: {RSI_LEVEL}\n"
        f"⚡ Workers: {MAX_WORKERS} (PARALEL)\n"
        f"⏰ Interval: {SCAN_INTERVAL}sn | Coins: {MAX_COINS}\n"
        f"\n"
        f"🟢 AL: Major zone YUKARI kirilir\n"
        f"🔴 SAT: Major zone ASAGI kirilir"
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
