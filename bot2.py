# -*- coding: utf-8 -*-
"""
TREND-ZONE SCANNER BOT (TEK DOSYA)
- 15dk grafikte SMA100 trend kirilimini arar
- 4H grafikte TEPE/DIP/MAJOR bolgelerini ve GERCEK GUNLUK PDH/PDL'i hesaplar
- Ikisi cakisirsa (destek/direnc bolgesinde kirilim) GUCLU sinyal olarak isaretler
- Ayarlar ve mimari (Telegram, Binance API, Render worker, paralel tarama,
  cooldown) mevcut calisan PEAK BREAKOUT SCANNER botunla ayni.
"""
import os
import time
import logging
from datetime import datetime
from dataclasses import dataclass, field
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
import pandas as pd
import numpy as np


logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════════════
# AYARLAR (mevcut botunla ayni env var isimleri)
# ══════════════════════════════════════════════════════════════════
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

SCAN_INTERVAL = int(os.getenv("SCAN_INTERVAL", "300"))
MAX_COINS = int(os.getenv("MAX_COINS", "600"))
SIGNAL_COOLDOWN = int(os.getenv("SIGNAL_COOLDOWN", "3600"))
MAX_WORKERS = int(os.getenv("MAX_WORKERS", "20"))
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "8"))

# Giris teyidi: 15dk SMA100 kirilimi
TIMEFRAME = os.getenv("TIMEFRAME", "15m")
KLINES_LIMIT = int(os.getenv("KLINES_LIMIT", "200"))
MAJOR_LINE_LEN = int(os.getenv("MAJOR_LINE_LEN", "100"))
PEAK_BREAK_PCT = float(os.getenv("PEAK_BREAK_PCT", "0.5"))

# 4H bolge ayarlari (TEPE/DIP/MAJOR + PDH/PDL)
ZONE_TIMEFRAME = os.getenv("ZONE_TIMEFRAME", "4h")
ZONE_BARS = int(os.getenv("ZONE_BARS", "50"))
AUTO_PEAK_PCT = float(os.getenv("AUTO_PEAK_PCT", "3.0"))
AUTO_DIP_PCT = float(os.getenv("AUTO_DIP_PCT", "3.0"))
AUTO_MAJOR_BINS = int(os.getenv("AUTO_MAJOR_BINS", "10"))

BINANCE_BASE = "https://fapi.binance.com"

last_signal = {}


# ══════════════════════════════════════════════════════════════════
# BINANCE VERI CEKME
# ══════════════════════════════════════════════════════════════════
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
    """Ham kline verisi - son satir CANLI (kapanmamis) mum olabilir."""
    try:
        session = requests.Session()
        r = session.get(
            f"{BINANCE_BASE}/fapi/v1/klines",
            params={"symbol": symbol, "interval": interval, "limit": limit},
            timeout=REQUEST_TIMEOUT,
        )
        raw = r.json()
        df = pd.DataFrame(raw, columns=[
            "open_time", "open", "high", "low", "close", "volume", "close_time",
            "qav", "trades", "tbv", "tqv", "ignore"])
        for c in ["open", "high", "low", "close", "volume"]:
            df[c] = df[c].astype(float)
        return df
    except Exception:
        return None


def get_klines_closed(symbol, interval, limit=200):
    """Sadece KAPANMIS mumlari doner - repaint/titresim onlenir."""
    df = get_klines(symbol, interval, limit=limit + 1)
    if df is None or len(df) < 2:
        return None
    return df.iloc[:-1].reset_index(drop=True)


# ══════════════════════════════════════════════════════════════════
# 4H TEPE / DIP / MAJOR BOLGE + GERCEK GUNLUK PDH/PDL
# ══════════════════════════════════════════════════════════════════
@dataclass
class Zones:
    peak_top: float
    peak_bot: float
    dip_top: float
    dip_bot: float
    major_top: float
    major_bot: float
    pdh: float
    pdl: float


def _major_bin(df, lowest, highest, bins):
    price_range = highest - lowest
    if price_range <= 0:
        return highest, lowest
    step = price_range / bins
    idx = ((df["close"] - lowest) / step).apply(np.floor).clip(0, bins - 1).astype(int)
    max_bin = int(idx.value_counts().idxmax())
    major_bot = lowest + step * max_bin
    major_top = lowest + step * (max_bin + 1)
    return major_top, major_bot


def compute_zones(symbol):
    df_zone = get_klines_closed(symbol, ZONE_TIMEFRAME, limit=ZONE_BARS + 5)
    if df_zone is None or len(df_zone) < ZONE_BARS:
        return None
    df_zone = df_zone.tail(ZONE_BARS)

    highest = df_zone["high"].max()
    lowest = df_zone["low"].min()

    peak_top = highest
    peak_bot = highest * (1 - AUTO_PEAK_PCT / 100)
    dip_top = lowest * (1 + AUTO_DIP_PCT / 100)
    dip_bot = lowest

    major_top, major_bot = _major_bin(df_zone, lowest, highest, AUTO_MAJOR_BINS)

    df_daily = get_klines_closed(symbol, "1d", limit=3)
    if df_daily is None or len(df_daily) < 1:
        return None
    prev_day = df_daily.iloc[-1]

    return Zones(
        peak_top=float(peak_top), peak_bot=float(peak_bot),
        dip_top=float(dip_top), dip_bot=float(dip_bot),
        major_top=float(major_top), major_bot=float(major_bot),
        pdh=float(prev_day["high"]), pdl=float(prev_day["low"]),
    )


def price_in_zone(price, top, bot):
    lo, hi = min(top, bot), max(top, bot)
    return lo <= price <= hi


# ══════════════════════════════════════════════════════════════════
# SINYAL MANTIGI: 15dk SMA100 kirilim + 4H bolge/PDH-PDL
# ══════════════════════════════════════════════════════════════════
@dataclass
class Signal:
    symbol: str
    side: str            # "AL" | "SAT"
    strong: bool
    price: float
    sma: float
    dist_pct: float
    bar_time: str
    reasons: list = field(default_factory=list)
    zones: Zones = None


def _sma(series, length):
    return float(series.tail(length).mean())


def analyze_symbol(symbol):
    df = get_klines_closed(symbol, TIMEFRAME, limit=KLINES_LIMIT)
    if df is None or len(df) < MAJOR_LINE_LEN + 2:
        return None

    close = df["close"]
    sma_now = _sma(close, MAJOR_LINE_LEN)
    sma_prev = _sma(close.iloc[:-1], MAJOR_LINE_LEN)

    price_now = float(close.iloc[-1])
    price_prev = float(close.iloc[-2])
    bar_time = str(df["open_time"].iloc[-1])

    dist_pct = abs(price_now - sma_now) / sma_now * 100

    crossed_up = price_prev <= sma_prev and price_now > sma_now
    crossed_down = price_prev >= sma_prev and price_now < sma_now

    break_up = crossed_up and dist_pct >= PEAK_BREAK_PCT
    break_down = crossed_down and dist_pct >= PEAK_BREAK_PCT

    if not break_up and not break_down:
        return None

    zones = compute_zones(symbol)  # sadece aday varsa hesapla (API tasarrufu)

    reasons = []
    strong = False

    if break_up:
        side = "AL"
        reasons.append(f"{TIMEFRAME} kapanis SMA{MAJOR_LINE_LEN}'u yukari kesti (mesafe %{dist_pct:.2f})")
        if zones:
            if price_in_zone(price_now, zones.dip_top, zones.dip_bot) or \
               price_in_zone(price_now, zones.major_top, zones.major_bot):
                strong = True
                reasons.append("Fiyat 4H DIP/MAJOR (destek) bolgesinde")
            if price_now > zones.pdh:
                strong = True
                reasons.append(f"Gunluk direnc (PDH: {zones.pdh:.6f}) kirildi")
    else:
        side = "SAT"
        reasons.append(f"{TIMEFRAME} kapanis SMA{MAJOR_LINE_LEN}'u asagi kesti (mesafe %{dist_pct:.2f})")
        if zones:
            if price_in_zone(price_now, zones.peak_top, zones.peak_bot) or \
               price_in_zone(price_now, zones.major_top, zones.major_bot):
                strong = True
                reasons.append("Fiyat 4H TEPE/MAJOR (direnc) bolgesinde")
            if price_now < zones.pdl:
                strong = True
                reasons.append(f"Gunluk destek (PDL: {zones.pdl:.6f}) kirildi")

    return Signal(
        symbol=symbol, side=side, strong=strong,
        price=price_now, sma=sma_now, dist_pct=dist_pct,
        bar_time=bar_time, reasons=reasons, zones=zones,
    )


# ══════════════════════════════════════════════════════════════════
# COOLDOWN
# ══════════════════════════════════════════════════════════════════
def should_send(symbol, side):
    key = f"{symbol}_{side}"
    now = time.time()
    if now - last_signal.get(key, 0) < SIGNAL_COOLDOWN:
        return False
    last_signal[key] = now
    return True


# ══════════════════════════════════════════════════════════════════
# TELEGRAM
# ══════════════════════════════════════════════════════════════════
def send_telegram(text):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log.error("TELEGRAM_TOKEN/TELEGRAM_CHAT_ID eksik, konsola yaziliyor:\n" + text)
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            data={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"},
            timeout=10,
        )
        return r.status_code == 200
    except Exception as e:
        log.error(f"Telegram hata: {e}")
        return False


def format_signal_message(signal: Signal):
    icon = "🚀" if signal.strong else ("📈" if signal.side == "AL" else "📉")
    title = "COK GUCLU" if signal.strong else "SINYAL"
    sep = "=" * 24
    coin = signal.symbol.replace("USDT", "/USDT")

    lines = [
        f"{icon} <b>{title} - {signal.side}</b>",
        sep,
        f"💱 <b>Coin:</b> {coin}",
        f"💰 <b>Fiyat:</b> {signal.price:.6f}",
        f"📍 <b>SMA{MAJOR_LINE_LEN} ({TIMEFRAME}):</b> {signal.sma:.6f}",
        f"📏 <b>Mesafe:</b> %{signal.dist_pct:.2f}",
        "",
        "🔎 <b>Gerekce:</b>",
    ]
    for r in signal.reasons:
        lines.append(f"• {r}")

    z = signal.zones
    if z:
        lines += [
            sep,
            f"🗺️ <b>4H Bolgeler ({ZONE_TIMEFRAME}):</b>",
            f"🔴 Tepe: {z.peak_bot:.6f} - {z.peak_top:.6f}",
            f"🟢 Dip: {z.dip_bot:.6f} - {z.dip_top:.6f}",
            f"🟡 Major: {z.major_bot:.6f} - {z.major_top:.6f}",
            f"PDH/PDL (gercek gunluk): {z.pdl:.6f} - {z.pdh:.6f}",
        ]
    lines += [sep, f"⏰ {datetime.now().strftime('%H:%M:%S %d/%m/%Y')}"]
    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════
# TARAMA DONGUSU (paralel, tum coinler)
# ══════════════════════════════════════════════════════════════════
def check_signal(symbol):
    try:
        signal = analyze_symbol(symbol)
        if signal is None:
            return None, {"symbol": symbol, "status": "no_signal"}
        if not should_send(symbol, signal.side):
            return None, {"symbol": symbol, "status": "cooldown"}
        return signal, {"symbol": symbol, "status": "signal"}
    except Exception as e:
        return None, {"symbol": symbol, "status": "error", "error": str(e)}


def run_scan_parallel():
    symbols = get_symbols()
    total = len(symbols)
    log.info(f"TARAMA BASLADI | Coin: {total} | Workers: {MAX_WORKERS} | Giris TF: {TIMEFRAME} | Bolge TF: {ZONE_TIMEFRAME}")

    stats = {"total": total, "signal": 0, "no_signal": 0, "cooldown": 0, "error": 0}
    found = []

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(check_signal, s): s for s in symbols}
        completed = 0
        for future in as_completed(futures):
            completed += 1
            try:
                signal, info = future.result()
                status = info["status"]
                stats[status] = stats.get(status, 0) + 1
                if status == "signal":
                    found.append(signal)
            except Exception as e:
                log.error(f"Future hata: {e}")
                stats["error"] += 1

            if completed % 100 == 0 or completed == total:
                log.info(f"[{completed}/{total}] Sinyal:{stats['signal']} NoSignal:{stats['no_signal']} Hata:{stats['error']}")

    for signal in found:
        try:
            msg = format_signal_message(signal)
            if send_telegram(msg):
                log.info(f"SINYAL GONDERILDI: {signal.symbol} {signal.side}")
            else:
                log.error(f"Telegram gonderilemedi: {signal.symbol}")
        except Exception as e:
            log.error(f"Gonderim hatasi {signal.symbol}: {e}")

    log.info(f"Tarama tamamlandi | {stats['signal']} sinyal | {stats}")
    return stats["signal"]


# ══════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════
def main():
    log.info("=" * 60)
    log.info("TREND-ZONE SCANNER BOT baslatildi")
    log.info(f"Max coin       : {MAX_COINS}")
    log.info(f"Workers        : {MAX_WORKERS}")
    log.info(f"Giris TF       : {TIMEFRAME}  (SMA{MAJOR_LINE_LEN})")
    log.info(f"Bolge TF       : {ZONE_TIMEFRAME}  (bar: {ZONE_BARS})")
    log.info(f"Tarama araligi : {SCAN_INTERVAL} sn")
    log.info(f"Cooldown       : {SIGNAL_COOLDOWN} sn")
    log.info("=" * 60)

    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log.error("TELEGRAM_TOKEN ve TELEGRAM_CHAT_ID eksik!")
        return

    send_telegram(
        "🚀 TREND-ZONE SCANNER BASLADI\n"
        + "=" * 30 + "\n"
        f"💱 Giris TF: {TIMEFRAME} (SMA{MAJOR_LINE_LEN})\n"
        f"🗺️ Bolge TF: {ZONE_TIMEFRAME}\n"
        f"⏰ Cooldown: {SIGNAL_COOLDOWN}sn\n"
        f"⚡ Workers: {MAX_WORKERS}\n\n"
        "🟢 AL: SMA100 yukari kirilim + 4H DIP/MAJOR destek ya da PDH kirilimi\n"
        "🔴 SAT: SMA100 asagi kirilim + 4H TEPE/MAJOR direnc ya da PDL kirilimi"
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
