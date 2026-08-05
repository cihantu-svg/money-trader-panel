# -*- coding: utf-8 -*-
"""
PURPLE ROSE BOT
Strateji: Pivot High/Low breakout + Govde kırılımı + Fitil filtresi + Hacim onayı
Zaman: 15m | Borsa: Binance Futures (USDT-M)
"""
import os
import time
import logging
import threading
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
import pandas as pd
import numpy as np
import urllib3
import warnings

urllib3.disable_warnings()
warnings.filterwarnings('ignore')

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s'
)
log = logging.getLogger(__name__)

# ============================================================
# AYARLAR (Render Environment Variables)
# ============================================================
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
SCAN_INTERVAL = int(os.environ.get("SCAN_INTERVAL_SEC", "180"))  # 3 dk
TIMEFRAME = os.environ.get("SCAN_TIMEFRAME", "15m")  # 5 dakika
MAX_COINS = int(os.environ.get("MAX_COINS", "600"))
MIN_VOLUME = float(os.environ.get("MIN_VOLUME_USDT", "5000000"))  # Min 5M USDT hacim

# Strateji parametreleri
PIVOT_LEN = int(os.environ.get("PIVOT_LEN", "5"))        # Pivot uzunluğu
REQUIRE_BODY = os.environ.get("REQUIRE_BODY", "true").lower() == "true"  # Sadece govde kırılımı
USE_WICK_FILTER = os.environ.get("USE_WICK_FILTER", "true").lower() == "true"
MAX_WICK_RATIO = float(os.environ.get("MAX_WICK_RATIO", "2.0"))  # Max fitil/govde oranı
USE_VOLUME_FILTER = os.environ.get("USE_VOLUME_FILTER", "true").lower() == "true"
VOL_PERIOD = int(os.environ.get("VOL_PERIOD", "20"))     # Hacim ortalama periyodu
MIN_VOL_MULT = float(os.environ.get("MIN_VOL_MULT", "0.8"))  # Min hacim çarpanı
SIGNAL_COOLDOWN = int(os.environ.get("SIGNAL_COOLDOWN", "1800"))  # 30 dk bekleme

# Paralel tarama
SCAN_WORKERS = int(os.environ.get("SCAN_WORKERS", "10"))
REQUESTS_PER_SEC = float(os.environ.get("REQUESTS_PER_SEC", "8"))
TELEGRAM_MSGS_PER_SEC = float(os.environ.get("TELEGRAM_MSGS_PER_SEC", "1.0"))

BINANCE_BASE = "https://fapi.binance.com"
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "PurpleRoseBot/1.0"})
_adapter = requests.adapters.HTTPAdapter(pool_connections=SCAN_WORKERS, pool_maxsize=SCAN_WORKERS * 2)
SESSION.mount("https://", _adapter)

sent_signals: dict = {}

_rate_lock = threading.Lock()
_next_slot = [0.0]


def _rate_limit_wait():
    with _rate_lock:
        now = time.time()
        wait = _next_slot[0] - now
        if wait > 0:
            time.sleep(wait)
            now = time.time()
        _next_slot[0] = max(now, _next_slot[0]) + 1.0 / REQUESTS_PER_SEC


_tg_lock = threading.Lock()
_tg_next_slot = [0.0]


def _telegram_rate_wait():
    with _tg_lock:
        now = time.time()
        wait = _tg_next_slot[0] - now
        if wait > 0:
            time.sleep(wait)
            now = time.time()
        _tg_next_slot[0] = max(now, _tg_next_slot[0]) + 1.0 / TELEGRAM_MSGS_PER_SEC


def get_symbols(min_volume: float = 0) -> list:
    try:
        r = SESSION.get(f"{BINANCE_BASE}/fapi/v1/ticker/24hr", timeout=30, verify=False)
        if r.status_code != 200:
            log.error(f"Sembol listesi alinamadi: HTTP {r.status_code}")
            return []
        data = r.json()
        exclude = ("3L", "3S", "5L", "5S", "UP", "DOWN", "BULL", "BEAR")
        symbols = []
        for t in data:
            sym = str(t.get("symbol", "")).upper()
            if not sym.endswith("USDT") or "_" in sym:
                continue
            if any(sym[:-4].endswith(x) for x in exclude):
                continue
            qv = float(t.get("quoteVolume") or 0)
            if qv < min_volume:
                continue
            symbols.append({
                "symbol": sym,
                "volume_24h": qv,
                "price": float(t.get("lastPrice") or 0),
                "change_24h": float(t.get("priceChangePercent") or 0),
            })
        symbols.sort(key=lambda x: x["volume_24h"], reverse=True)
        return symbols
    except Exception as e:
        log.error(f"get_symbols hata: {e}")
        return []


def get_klines(symbol: str, interval: str = "15m", limit: int = 200):
    _rate_limit_wait()
    try:
        r = SESSION.get(
            f"{BINANCE_BASE}/fapi/v1/klines",
            params={"symbol": symbol, "interval": interval, "limit": limit},
            timeout=15, verify=False
        )
        if r.status_code == 429 or r.status_code == 418:
            log.warning(f"Rate limit uyarisi ({r.status_code}) {symbol}")
            return None
        if r.status_code != 200:
            return None
        data = r.json()
        if not isinstance(data, list) or len(data) < PIVOT_LEN * 4 + 10:
            return None
        rows = []
        for k in data:
            try:
                rows.append({
                    "ts": pd.to_datetime(int(k[0]), unit="ms"),
                    "Open": float(k[1]),
                    "High": float(k[2]),
                    "Low": float(k[3]),
                    "Close": float(k[4]),
                    "Volume": float(k[5]),
                })
            except Exception:
                continue
        if len(rows) < 50:
            return None
        df = pd.DataFrame(rows).set_index("ts")
        return df[["Open", "High", "Low", "Close", "Volume"]].dropna()
    except Exception:
        return None


def find_pivot_highs(high: pd.Series, left: int, right: int):
    """Pine Script ta.pivothigh birebir implementasyonu"""
    pivots = []
    for i in range(left, len(high) - right):
        window = high.iloc[i - left:i + right + 1]
        if high.iloc[i] == window.max() and high.iloc[i] > high.iloc[i - 1]:
            pivots.append((i, high.iloc[i]))
    return pivots


def find_pivot_lows(low: pd.Series, left: int, right: int):
    """Pine Script ta.pivotlow birebir implementasyonu"""
    pivots = []
    for i in range(left, len(low) - right):
        window = low.iloc[i - left:i + right + 1]
        if low.iloc[i] == window.min() and low.iloc[i] < low.iloc[i - 1]:
            pivots.append((i, low.iloc[i]))
    return pivots


def check_signal(df: pd.DataFrame, symbol: str, locked_state: dict) -> list:
    """
    PURPLE ROSE Stratejisi:
    - Pivot High/Low tespiti
    - Govde ile kırılım
    - Fitil filtresi (spike rejection)
    - Hacim filtresi
    """
    if df is None or len(df) < PIVOT_LEN * 4 + 10:
        return []

    close = df["Close"]
    high = df["High"]
    low = df["Low"]
    opn = df["Open"]
    volume = df["Volume"]

    i = -2  # Son kapanmis mum (repaint yok)

    def safe(s, idx, default=0.0):
        try:
            v = s.iloc[idx]
            return float(v) if not pd.isna(v) else default
        except Exception:
            return default

    cur_close = safe(close, i)
    cur_open = safe(opn, i)
    cur_high = safe(high, i)
    cur_low = safe(low, i)
    cur_vol = safe(volume, i)

    if cur_close <= 0 or cur_open <= 0:
        return []

    # === PİVOT TESPİTİ ===
    # Son N mumda pivot high/low ara
    lookback = min(100, len(df) - PIVOT_LEN * 2)
    ph_list = find_pivot_highs(high.iloc[-lookback:], PIVOT_LEN, PIVOT_LEN)
    pl_list = find_pivot_lows(low.iloc[-lookback:], PIVOT_LEN, PIVOT_LEN)

    # Son pivot seviyeleri
    locked_ph = ph_list[-1][1] if ph_list else None
    locked_pl = pl_list[-1][1] if pl_list else None

    if locked_ph is None and locked_pl is None:
        return []

    # === FİTİL FİLTRESİ ===
    candle_body = abs(cur_close - cur_open)
    upper_wick = cur_high - max(cur_close, cur_open)
    lower_wick = min(cur_close, cur_open) - cur_low

    is_spike_long = USE_WICK_FILTER and candle_body > 0 and (upper_wick > candle_body * MAX_WICK_RATIO)
    is_spike_short = USE_WICK_FILTER and candle_body > 0 and (lower_wick > candle_body * MAX_WICK_RATIO)

    # === HACİM FİLTRESİ ===
    vol_sma = volume.rolling(window=VOL_PERIOD).mean().iloc[i]
    volume_ok = not USE_VOLUME_FILTER or (cur_vol >= vol_sma * MIN_VOL_MULT)

    # === SİNYAL KOŞULLARI ===
    results = []

    # LONG: Direnç gövde ile yukarı kırıldı
    if locked_ph is not None:
        if REQUIRE_BODY:
            body_break_long = min(cur_close, cur_open) > locked_ph
        else:
            body_break_long = cur_close > locked_ph

        # Önceki mum seviyenin altında mıydı? (crossover kontrolü)
        prev_close = safe(close, i - 1)
        prev_below = prev_close <= locked_ph

        if body_break_long and prev_below and not is_spike_long and volume_ok:
            results.append({
                "direction": "AL",
                "type": "PURPLE_ROSE_AL",
                "price": cur_close,
                "pivot_level": round(locked_ph, 8),
                "body": round(candle_body, 8),
                "upper_wick": round(upper_wick, 8),
                "lower_wick": round(lower_wick, 8),
                "volume_ratio": round(cur_vol / vol_sma, 2) if vol_sma > 0 else 0,
            })

    # SHORT: Destek gövde ile aşağı kırıldı
    if locked_pl is not None:
        if REQUIRE_BODY:
            body_break_short = max(cur_close, cur_open) < locked_pl
        else:
            body_break_short = cur_close < locked_pl

        prev_close = safe(close, i - 1)
        prev_above = prev_close >= locked_pl

        if body_break_short and prev_above and not is_spike_short and volume_ok:
            results.append({
                "direction": "SAT",
                "type": "PURPLE_ROSE_SAT",
                "price": cur_close,
                "pivot_level": round(locked_pl, 8),
                "body": round(candle_body, 8),
                "upper_wick": round(upper_wick, 8),
                "lower_wick": round(lower_wick, 8),
                "volume_ratio": round(cur_vol / vol_sma, 2) if vol_sma > 0 else 0,
            })

    return results


def send_telegram(message: str) -> bool:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log.warning("Telegram token veya chat_id eksik!")
        return False
    _telegram_rate_wait()
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"},
            timeout=10
        )
        return r.status_code == 200
    except Exception as e:
        log.error(f"Telegram hata: {e}")
        return False


def format_message(symbol: str, sig: dict) -> str:
    yon = sig["direction"]
    if yon == "AL":
        bas = "AL SINYALI (YUKARI KIRILIM)"
    else:
        bas = "SAT SINYALI (ASAGI KIRILIM)"

    coin = symbol.replace("USDT", "/USDT")

    lines = [
        bas,
        "-" * 20,
        f"Coin: {coin}",
        f"Zaman: {TIMEFRAME}",
        f"Pivot Kırılım: {sig['type']}",
        "-" * 20,
        f"Fiyat: {sig['price']}",
        f"Pivot Seviyesi: {sig['pivot_level']}",
        f"Govde: {sig['body']}",
        f"Ust Fitil: {sig['upper_wick']}",
        f"Alt Fitil: {sig['lower_wick']}",
        f"Hacim Orani: {sig['volume_ratio']}x",
        "-" * 20,
        datetime.now().strftime('%H:%M:%S %d/%m/%Y')
    ]
    return "\n".join(lines)


def should_send(symbol: str, sig_type: str) -> bool:
    key = f"{symbol}_{sig_type}"
    now = time.time()
    if key in sent_signals and (now - sent_signals[key]) < SIGNAL_COOLDOWN:
        return False
    sent_signals[key] = now
    return True


def _scan_one(coin):
    symbol = coin["symbol"]
    try:
        limit = max(PIVOT_LEN * 4 + 20, 100)
        df = get_klines(symbol, TIMEFRAME, limit=limit)
        sigs = check_signal(df, symbol, {})
        return symbol, sigs
    except Exception as e:
        log.error(f"{symbol} hata: {e}")
        return symbol, []


def run_scan():
    log.info(f"Tarama basladi TF:{TIMEFRAME} PivotLen:{PIVOT_LEN} Max:{MAX_COINS}")

    symbols = get_symbols(min_volume=MIN_VOLUME)
    if not symbols:
        log.error("Coin listesi alinamadi!")
        return

    symbols = symbols[:MAX_COINS]
    total = len(symbols)
    found = 0
    scanned = 0
    t0 = time.time()

    with ThreadPoolExecutor(max_workers=SCAN_WORKERS) as pool:
        futures = {pool.submit(_scan_one, coin): coin["symbol"] for coin in symbols}

        for future in as_completed(futures):
            symbol = futures[future]
            scanned += 1
            try:
                _, sigs = future.result()
            except Exception as e:
                log.error(f"{symbol} beklenmeyen hata: {e}")
                continue

            for sig in sigs:
                if should_send(symbol, sig["type"]):
                    msg = format_message(symbol, sig)
                    if send_telegram(msg):
                        log.info(f"OK {symbol} {sig['type']} pivot:{sig['pivot_level']}")
                        found += 1

            if scanned % 50 == 0:
                log.info(f"[{scanned}/{total}] tarandi {found} sinyal")

    elapsed = time.time() - t0
    log.info(f"Tarama tamamlandi {found} sinyal gonderildi ({elapsed:.1f}sn)")


def main():
    log.info("=" * 55)
    log.info("PURPLE ROSE BOT baslatildi")
    log.info(f"  Strateji : Pivot High/Low breakout + Govde + Fitil + Hacim")
    log.info(f"  Zaman    : {TIMEFRAME}")
    log.info(f"  Min Vol  : {MIN_VOLUME} USDT")
    log.info(f"  Aralik   : her {SCAN_INTERVAL} saniye")
    log.info(f"  Max coin : {MAX_COINS}")
    log.info(f"  Paralel  : {SCAN_WORKERS} worker, {REQUESTS_PER_SEC} istek/sn")
    log.info("=" * 55)

    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log.error("TELEGRAM_TOKEN ve TELEGRAM_CHAT_ID eksik!")
        return

    send_telegram(
        f"PURPLE ROSE BOT BASLADI\n"
        f"Strateji: Pivot Breakout + Govde + Fitil + Hacim\n"
        f"Zaman: {TIMEFRAME} | Min Hacim: {MIN_VOLUME} USDT\n"
        f"Aralik: {SCAN_INTERVAL//60} dk | Max Coin: {MAX_COINS}\n"
        f"{datetime.now().strftime('%H:%M:%S %d/%m/%Y')}"
    )

    while True:
        try:
            run_scan()
        except Exception as e:
            log.error(f"Dongu hatasi: {e}")
            send_telegram(f"Bot Hatasi: {e}")

        log.info(f"Sonraki tarama {SCAN_INTERVAL} saniye sonra...")
        time.sleep(SCAN_INTERVAL)


if __name__ == "__main__":
    main()
