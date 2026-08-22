# -*- coding: utf-8 -*-
"""
SMA100 HIZLI KIRILIM + RSI(50) + FIBONACCI(%50/100 mum) SCANNER (SADECE LONG)

MANTIK (backtest ile dogrulandi: 60 gunluk veri, 185 sinyal,
%10 hit-rate ~%60, ort. lehte hareket ~%27.7):

Son KAPANMIS 15dk mumda UC sart AYNI ANDA aranir:
  1) SMA100 HIZLI KIRILIM: son 2-3 mumda fiyat SMA100'e yakinken
     (uzaklik <= NEAR_TOL_PCT) bu mumda SMA100'un %4+ ustune firlamis
     (yeni kirilim - bir onceki mum zaten kirilim sartini saglamiyor)
  2) RSI TARAFI: RSI(14) bu mumda >= 50 (kesisim sarti yok, sadece taraf)
  3) FIBONACCI KESISIMI: son 100 mumun (bu mum haric) en yuksek/en dusuk
     noktasi arasindaki %50 seviyesi, bu mumda YUKARI dogru kesilmis
     (onceki mum bu seviyenin altinda, bu mum ustunde)

SADECE LONG sinyali uretilir - backtest'te SHORT tarafinin bu filtreyle
baseline'in bile altina dustugu goruldugu icin SHORT devre disi birakildi.

SADECE KAPANMIS mumlar kullanilir (repaint yok).
"""
import os
import time
import logging
from datetime import datetime
from dataclasses import dataclass
from concurrent.futures import ThreadPoolExecutor, as_completed

import numpy as np
import requests
import pandas as pd

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════════════
# AYARLAR
# ══════════════════════════════════════════════════════════════════
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

SCAN_INTERVAL = int(os.getenv("SCAN_INTERVAL", "180"))     # 15dk mum, sik taramaya gerek yok
MAX_COINS = int(os.getenv("MAX_COINS", "600"))
SIGNAL_COOLDOWN = int(os.getenv("SIGNAL_COOLDOWN", "3600"))
MAX_WORKERS = int(os.getenv("MAX_WORKERS", "15"))
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "10"))

TIMEFRAME = os.getenv("TIMEFRAME", "15m")

# --- STRATEJI PARAMETRELERI (backtest ile ayni) ---
SMA_PERIOD = int(os.getenv("SMA_PERIOD", "100"))
NEAR_TOL_PCT = float(os.getenv("NEAR_TOL_PCT", "1.5"))
BREAK_PCT = float(os.getenv("BREAK_PCT", "4.0"))
LOOKBACK_WINDOW = int(os.getenv("LOOKBACK_WINDOW", "3"))
RSI_PERIOD = int(os.getenv("RSI_PERIOD", "14"))
FIB_WINDOW = int(os.getenv("FIB_WINDOW", "100"))

# --- LIKIDITE FILTRESI ---
USE_LIQUIDITY_FILTER = os.getenv("USE_LIQUIDITY_FILTER", "true").lower() == "true"
MIN_QUOTE_VOLUME_24H = float(os.getenv("MIN_QUOTE_VOLUME_24H", "3000000"))  # 3 milyon USDT

# --- SINYAL SAYISI SINIRI ---
MAX_SIGNALS_PER_SCAN = int(os.getenv("MAX_SIGNALS_PER_SCAN", "5"))

# --- RETRY AYARLARI ---
MAX_RETRIES = int(os.getenv("MAX_RETRIES", "3"))
RETRY_BACKOFF_BASE = float(os.getenv("RETRY_BACKOFF_BASE", "0.5"))

BINANCE_BASE = "https://fapi.binance.com"
last_signal = {}

# SMA100 + FIB100 (shift'li) icin yeterli gecmis + LOOKBACK_WINDOW + guvenlik payi
KLINES_LIMIT = int(os.getenv("KLINES_LIMIT", "150"))

# ══════════════════════════════════════════════════════════════════
# GLOBAL SESSION + RETRY
# ══════════════════════════════════════════════════════════════════
session = requests.Session()
session.headers.update({"Connection": "keep-alive"})


def _request_with_retry(url, params=None, timeout=REQUEST_TIMEOUT):
    last_exc = None
    for attempt in range(MAX_RETRIES + 1):
        try:
            r = session.get(url, params=params, timeout=timeout)
            if r.status_code == 200:
                return r
            if r.status_code in (429, 418) or r.status_code >= 500:
                wait = RETRY_BACKOFF_BASE * (2 ** attempt)
                retry_after = r.headers.get("Retry-After")
                if retry_after:
                    try:
                        wait = max(wait, float(retry_after))
                    except ValueError:
                        pass
                log.warning(f"HTTP {r.status_code} ({url.split('/')[-1]}), {wait:.1f}sn sonra tekrar "
                            f"(deneme {attempt+1}/{MAX_RETRIES+1})")
                time.sleep(wait)
                last_exc = Exception(f"HTTP {r.status_code}")
                continue
            r.raise_for_status()
        except requests.exceptions.RequestException as e:
            last_exc = e
            wait = RETRY_BACKOFF_BASE * (2 ** attempt)
            time.sleep(wait)
    if last_exc:
        raise last_exc
    raise Exception("Bilinmeyen istek hatasi")


# ══════════════════════════════════════════════════════════════════
# BINANCE VERI CEKME
# ══════════════════════════════════════════════════════════════════
def get_symbols():
    try:
        r = _request_with_retry(f"{BINANCE_BASE}/fapi/v1/exchangeInfo", timeout=10)
        data = r.json()
        syms = [s["symbol"] for s in data["symbols"]
                if s["symbol"].endswith("USDT") and s["status"] == "TRADING"]
        return syms[:MAX_COINS]
    except Exception as e:
        log.error(f"get_symbols hata: {e}")
        return []


def get_klines(symbol, interval, limit=200):
    try:
        r = _request_with_retry(
            f"{BINANCE_BASE}/fapi/v1/klines",
            params={"symbol": symbol, "interval": interval, "limit": limit},
            timeout=REQUEST_TIMEOUT,
        )
        raw = r.json()
        df = pd.DataFrame(raw, columns=[
            "open_time", "open", "high", "low", "close", "volume", "close_time",
            "qav", "trades", "taker_buy_base", "taker_buy_quote", "ignore"])
        for c in ["open", "high", "low", "close", "volume"]:
            df[c] = df[c].astype(float)
        return df
    except Exception as e:
        log.debug(f"get_klines hata ({symbol}): {e}")
        return None


def get_klines_closed(symbol, interval, limit=200):
    """Sadece KAPANMIS mumlari doner - repaint/titresim onlenir."""
    df = get_klines(symbol, interval, limit=limit + 1)
    if df is None or len(df) < 2:
        return None
    return df.iloc[:-1].reset_index(drop=True)


def get_all_24h_quote_volumes():
    try:
        r = _request_with_retry(f"{BINANCE_BASE}/fapi/v1/ticker/24hr", timeout=15)
        data = r.json()
        return {d["symbol"]: float(d.get("quoteVolume", 0)) for d in data}
    except Exception as e:
        log.error(f"get_all_24h_quote_volumes hata: {e}")
        return {}


def compute_rsi(close, period=14):
    delta = close.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = -delta.where(delta < 0, 0.0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


# ══════════════════════════════════════════════════════════════════
# SINYAL: SMA100 KIRILIM + RSI(50) + FIB(100) - SADECE LONG
# ══════════════════════════════════════════════════════════════════
@dataclass
class Signal:
    symbol: str
    price: float
    distance_pct: float     # SMA100'e uzaklik (%) - kirilim buyuklugu
    rsi: float
    fib_level: float
    bar_time: str


def analyze_symbol(symbol, quote_volumes=None):
    if USE_LIQUIDITY_FILTER:
        qv = (quote_volumes or {}).get(symbol, 0.0)
        if qv < MIN_QUOTE_VOLUME_24H:
            return None

    min_needed = max(SMA_PERIOD, FIB_WINDOW) + LOOKBACK_WINDOW + 2
    df = get_klines_closed(symbol, TIMEFRAME, limit=KLINES_LIMIT)
    if df is None or len(df) < min_needed:
        return None

    close = df["close"].astype(float)
    high = df["high"].astype(float)
    low = df["low"].astype(float)

    sma = close.rolling(SMA_PERIOD).mean()
    distance_pct = (close - sma) / sma * 100
    rsi = compute_rsi(close, RSI_PERIOD)

    roll_high = high.rolling(FIB_WINDOW).max().shift(1)
    roll_low = low.rolling(FIB_WINDOW).min().shift(1)
    fib50 = (roll_high + roll_low) / 2

    i = len(df) - 1  # son KAPANMIS mum

    d_now = distance_pct.iloc[i]
    if pd.isna(d_now):
        return None

    # 1) SMA100 HIZLI KIRILIM
    recent = distance_pct.iloc[i - LOOKBACK_WINDOW:i]
    if recent.isna().any():
        return None
    was_near = recent.abs().min() <= NEAR_TOL_PCT
    if not (was_near and d_now >= BREAK_PCT):
        return None

    d_prev = distance_pct.iloc[i - 1]
    if not pd.isna(d_prev) and d_prev >= BREAK_PCT:
        return None  # yeni kirilim degil, onceki mum zaten kirmis

    # 2) RSI TARAFI (>= 50)
    r_now = rsi.iloc[i]
    if pd.isna(r_now) or r_now < 50:
        return None

    # 3) FIBONACCI KESISIMI (yukari dogru, bu mumda)
    level = fib50.iloc[i]
    if pd.isna(level):
        return None
    close_prev = float(close.iloc[i - 1])
    close_now = float(close.iloc[i])
    if not (close_prev < level <= close_now):
        return None

    return Signal(
        symbol=symbol,
        price=close_now,
        distance_pct=float(d_now),
        rsi=float(r_now),
        fib_level=float(level),
        bar_time=datetime.fromtimestamp(int(df["open_time"].iloc[i]) / 1000).strftime("%d/%m/%Y %H:%M:%S"),
    )


# ══════════════════════════════════════════════════════════════════
# COOLDOWN
# ══════════════════════════════════════════════════════════════════
def should_send(symbol):
    now = time.time()
    if now - last_signal.get(symbol, 0) < SIGNAL_COOLDOWN:
        return False
    last_signal[symbol] = now
    return True


# ══════════════════════════════════════════════════════════════════
# TELEGRAM
# ══════════════════════════════════════════════════════════════════
def send_telegram(text):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log.error("TELEGRAM_TOKEN/TELEGRAM_CHAT_ID eksik, konsola yaziliyor:\n" + text)
        return False
    try:
        r = session.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            data={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"},
            timeout=10,
        )
        return r.status_code == 200
    except Exception as e:
        log.error(f"Telegram hata: {e}")
        return False


def format_signal_message(signal: Signal):
    coin = signal.symbol.replace("USDT", "/USDT")
    sep = "=" * 24
    lines = [
        "🟢 <b>SMA100 KIRILIM + RSI + FIB ONAYLI LONG</b>",
        sep,
        f"💱 <b>Coin:</b> {coin}",
        f"💰 <b>Fiyat:</b> {signal.price:.6f}",
        f"🚀 <b>SMA100'e Uzaklik:</b> +%{signal.distance_pct:.2f}",
        f"📊 <b>RSI(14):</b> {signal.rsi:.1f}",
        f"📐 <b>Fib100 Seviyesi:</b> {signal.fib_level:.6f} (yukari kesildi)",
        sep,
        f"🕐 <b>Mum Zamani:</b> {signal.bar_time}",
        f"⏰ {datetime.now().strftime('%H:%M:%S %d/%m/%Y')}",
    ]
    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════
# TARAMA DONGUSU (paralel, tum coinler)
# ══════════════════════════════════════════════════════════════════
def check_signal(symbol, quote_volumes=None):
    try:
        signal = analyze_symbol(symbol, quote_volumes=quote_volumes)
        if signal is None:
            return None, {"symbol": symbol, "status": "no_signal"}
        if not should_send(symbol):
            return None, {"symbol": symbol, "status": "cooldown"}
        return signal, {"symbol": symbol, "status": "signal"}
    except Exception as e:
        return None, {"symbol": symbol, "status": "error", "error": str(e)}


def run_scan_parallel():
    symbols = get_symbols()
    total = len(symbols)
    log.info(f"TARAMA BASLADI | Coin: {total} | Workers: {MAX_WORKERS} | TF: {TIMEFRAME} | "
              f"SMA{SMA_PERIOD} kirilim>=%{BREAK_PCT} | RSI>=50 | Fib{FIB_WINDOW}")

    quote_volumes = get_all_24h_quote_volumes() if USE_LIQUIDITY_FILTER else {}

    stats = {"total": total, "signal": 0, "no_signal": 0, "cooldown": 0, "error": 0}
    found = []

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(check_signal, s, quote_volumes): s for s in symbols}
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

    # --- En guclu kirilimlari (SMA100'e uzaklik buyuklugune gore) one al ---
    found.sort(key=lambda s: s.distance_pct, reverse=True)
    if MAX_SIGNALS_PER_SCAN > 0 and len(found) > MAX_SIGNALS_PER_SCAN:
        log.info(f"{len(found)} sinyal bulundu, en guclu {MAX_SIGNALS_PER_SCAN} tanesi gonderiliyor")
        found = found[:MAX_SIGNALS_PER_SCAN]

    for signal in found:
        try:
            msg = format_signal_message(signal)
            if send_telegram(msg):
                log.info(f"SINYAL GONDERILDI: {signal.symbol} LONG "
                          f"distance={signal.distance_pct:.2f}% rsi={signal.rsi:.1f}")
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
    log.info("SMA100 KIRILIM + RSI(50) + FIB(100) SCANNER (SADECE LONG) baslatildi")
    log.info(f"Max coin         : {MAX_COINS}")
    log.info(f"Workers          : {MAX_WORKERS}")
    log.info(f"TF               : {TIMEFRAME}")
    log.info(f"SMA periyodu     : {SMA_PERIOD} | kirilim esigi: %{BREAK_PCT} | yakinlik: %{NEAR_TOL_PCT}")
    log.info(f"RSI periyodu     : {RSI_PERIOD} (>= 50 sarti)")
    log.info(f"Fib penceresi    : {FIB_WINDOW} mum")
    log.info(f"Likidite filtre  : {USE_LIQUIDITY_FILTER} (min {MIN_QUOTE_VOLUME_24H/1e6:.1f}M USDT)")
    log.info(f"Max sinyal/tarama: {MAX_SIGNALS_PER_SCAN if MAX_SIGNALS_PER_SCAN > 0 else 'sinirsiz'}")
    log.info(f"Cooldown         : {SIGNAL_COOLDOWN} sn")
    log.info(f"Tarama araligi   : {SCAN_INTERVAL} sn")
    log.info("=" * 60)

    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log.error("TELEGRAM_TOKEN ve TELEGRAM_CHAT_ID eksik!")
        return

    send_telegram(
        "🚀 SMA100 KIRILIM + RSI + FIB SCANNER (SADECE LONG) BASLADI\n"
        + "=" * 30 + "\n"
        f"💱 TF: {TIMEFRAME}\n"
        f"🚀 SMA{SMA_PERIOD} kirilim esigi: %{BREAK_PCT}\n"
        f"📊 RSI({RSI_PERIOD}) >= 50\n"
        f"📐 Fibonacci penceresi: {FIB_WINDOW} mum\n"
        f"💧 Min likidite: {MIN_QUOTE_VOLUME_24H/1e6:.1f}M USDT\n"
        f"⏰ Cooldown: {SIGNAL_COOLDOWN}sn\n"
        f"⚡ Workers: {MAX_WORKERS}"
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
