# -*- coding: utf-8 -*-
"""
MAJOR KIRILIM BOT
Strateji: SMA100 kesisim + govde buyuklugu + kisa fitil
Zaman: 1h | Tarama: 3dk | Borsa: Binance Futures
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
TIMEFRAME = os.environ.get("SCAN_TIMEFRAME", "1h")
MAX_COINS = int(os.environ.get("MAX_COINS", "600"))
MIN_VOLUME = float(os.environ.get("MIN_VOLUME_USDT", "1000000"))

MAJOR_LEN = int(os.environ.get("MAJOR_LEN", "100"))
SPANB_LEN = int(os.environ.get("SPANB_LEN", "52"))
BODY_PCT_MIN = float(os.environ.get("BODY_PCT_MIN", "4.0"))
MAX_LINE_GAP_PCT = float(os.environ.get("MAX_LINE_GAP_PCT", "1.0"))
SIGNAL_COOLDOWN = int(os.environ.get("SIGNAL_COOLDOWN", "3600"))

# YENI: Fitil onayi
LOW_WICK_MAX = float(os.environ.get("LOW_WICK_MAX", "25.0"))
HIGH_WICK_MAX = float(os.environ.get("HIGH_WICK_MAX", "25.0"))

SCAN_WORKERS = int(os.environ.get("SCAN_WORKERS", "10"))
REQUESTS_PER_SEC = float(os.environ.get("REQUESTS_PER_SEC", "8"))
TELEGRAM_MSGS_PER_SEC = float(os.environ.get("TELEGRAM_MSGS_PER_SEC", "1.0"))

BINANCE_BASE = "https://fapi.binance.com"
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "MajorKirilimBot/1.0"})
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


def calc_sma(series: pd.Series, period: int) -> pd.Series:
    return series.rolling(window=period).mean()


def calc_spanb(high: pd.Series, low: pd.Series, period: int) -> pd.Series:
    return (high.rolling(window=period).max() + low.rolling(window=period).min()) / 2


def calc_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    h, l, c = df['High'], df['Low'], df['Close']
    tr = pd.concat([h - l, abs(h - c.shift()), abs(l - c.shift())], axis=1).max(axis=1)
    return tr.rolling(window=period).mean()


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
            log.warning(f"Rate limit uyarisi ({r.status_code}) {symbol} - bu tur atlaniyor")
            return None
        if r.status_code != 200:
            return None
        data = r.json()
        if not isinstance(data, list) or len(data) < max(MAJOR_LEN, SPANB_LEN) + 5:
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


def check_signal(df: pd.DataFrame, symbol: str) -> list:
    """
    AL : Govdesi >= BODY_PCT_MIN olan YESIL mum, SMA100 yukari keserse
         VE alt fitili kisa (low, body_bottom'dan max LOW_WICK_MAX % asagida)
    SAT: Govdesi >= BODY_PCT_MIN olan KIRMIZI mum, SMA100 asagi keserse
         VE ust fitili kisa (high, body_top'dan max HIGH_WICK_MAX % yukarida)
    """
    if df is None or len(df) < MAJOR_LEN + 5:
        return []

    close = df["Close"]
    high = df["High"]
    low = df["Low"]
    opn = df["Open"]

    major = calc_sma(close, MAJOR_LEN)
    spanb = calc_spanb(high, low, SPANB_LEN)
    atr = calc_atr(df, 14)

    i, ip = -2, -3

    def safe(s, idx, default=0.0):
        try:
            v = s.iloc[idx]
            return float(v) if not pd.isna(v) else default
        except Exception:
            return default

    cur_close = safe(close, i)
    prev_close = safe(close, ip)
    cur_open = safe(opn, i)
    cur_high = safe(high, i)
    cur_low = safe(low, i)
    cur_major = safe(major, i)
    prev_major = safe(major, ip)
    cur_spanb = safe(spanb, i)
    prev_spanb = safe(spanb, ip)
    cur_atr = safe(atr, i, cur_close * 0.01)

    if cur_major <= 0 or cur_spanb <= 0 or cur_open <= 0:
        return []

    body_size = abs(cur_close - cur_open)
    body_bottom = min(cur_close, cur_open)
    body_top = max(cur_close, cur_open)

    # Fitil uzunlugu: body_size'a gore yuzde
    if cur_low < body_bottom and body_size > 0:
        low_wick_pct = (body_bottom - cur_low) / body_size * 100
    else:
        low_wick_pct = 0.0

    if cur_high > body_top and body_size > 0:
        high_wick_pct = (cur_high - body_top) / body_size * 100
    else:
        high_wick_pct = 0.0

    dist_major = (cur_close - cur_major) / cur_major * 100
    dist_spanb = (cur_close - cur_spanb) / cur_spanb * 100
    line_gap_pct = abs(cur_major - cur_spanb) / cur_close * 100

    cross_major_up = (cur_close > cur_major) and (prev_close <= prev_major)
    cross_major_dn = (cur_close < cur_major) and (prev_close >= prev_major)

    body_pct = body_size / cur_open * 100
    candle_green = cur_close > cur_open
    candle_red = cur_close < cur_open
    body_ok = body_pct >= BODY_PCT_MIN

    # Fitil onayi
    wick_ok_al = low_wick_pct <= LOW_WICK_MAX
    wick_ok_sat = high_wick_pct <= HIGH_WICK_MAX

    signal_al = cross_major_up and candle_green and body_ok and wick_ok_al
    signal_sat = cross_major_dn and candle_red and body_ok and wick_ok_sat

    results = []

    if signal_al:
        hedef = cur_close + cur_atr * 2
        results.append({
            "direction": "AL",
            "type": "KIRILIM_AL",
            "kirilim": "SMA100 Kesisim + Kisa Fitil",
            "price": cur_close,
            "major": round(cur_major, 8),
            "spanb": round(cur_spanb, 8),
            "dist_major": round(dist_major, 2),
            "dist_spanb": round(dist_spanb, 2),
            "hedef": round(hedef, 8),
            "beklenti": round((hedef - cur_close) / cur_close * 100, 2),
            "govde_yuzde": round(body_pct, 2),
            "line_gap": round(line_gap_pct, 2),
            "wick_pct": round(low_wick_pct, 2),
        })

    if signal_sat:
        hedef = cur_close - cur_atr * 2
        results.append({
            "direction": "SAT",
            "type": "KIRILIM_SAT",
            "kirilim": "SMA100 Kesisim + Kisa Fitil",
            "price": cur_close,
            "major": round(cur_major, 8),
            "spanb": round(cur_spanb, 8),
            "dist_major": round(dist_major, 2),
            "dist_spanb": round(dist_spanb, 2),
            "hedef": round(hedef, 8),
            "beklenti": round((cur_close - hedef) / cur_close * 100, 2),
            "govde_yuzde": round(body_pct, 2),
            "line_gap": round(line_gap_pct, 2),
            "wick_pct": round(high_wick_pct, 2),
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
        bas = "AL SINYALI (YUKARI)"
        wick_info = f"Alt Fitil: %{sig.get('wick_pct', 0)} (body)"
    else:
        bas = "SAT SINYALI (ASAGI)"
        wick_info = f"Ust Fitil: %{sig.get('wick_pct', 0)} (body)"

    coin = symbol.replace("USDT", "/USDT")
    sep = "-" * 20

    lines = [
        bas,
        sep,
        f"Coin: {coin}",
        f"Zaman: {TIMEFRAME} | Kirilim: {sig.get('kirilim', '-')}",
        sep,
        f"Fiyat: {sig['price']} | Hedef: {sig['hedef']} (%{sig['beklenti']})",
        f"Govde: %{sig.get('govde_yuzde', 0)} | {wick_info}",
        f"Cizgi Araligi: %{sig.get('line_gap', 0)}",
        sep,
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
        limit = max(MAJOR_LEN, SPANB_LEN) + 20
        df = get_klines(symbol, TIMEFRAME, limit=limit)
        sigs = check_signal(df, symbol)
        return symbol, sigs
    except Exception as e:
        log.error(f"{symbol} hata: {e}")
        return symbol, []


def run_scan():
    log.info(f"Tarama basladi TF:{TIMEFRAME} GovdeMin:%{BODY_PCT_MIN} Max:{MAX_COINS} Paralel:{SCAN_WORKERS}")

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
                        log.info(f"OK {symbol} {sig['type']} govde %{sig['govde_yuzde']}")
                        found += 1

            if scanned % 50 == 0:
                log.info(f"[{scanned}/{total}] tarandi {found} sinyal")

    elapsed = time.time() - t0
    log.info(f"Tarama tamamlandi {found} sinyal gonderildi ({elapsed:.1f}sn)")


def main():
    log.info("=" * 55)
    log.info("MAJOR KIRILIM BOT baslatildi")
    log.info(f"  Strateji : SMA{MAJOR_LEN} kesisim + govde >= %{BODY_PCT_MIN}")
    log.info(f"  Fitil    : AL alt <= %{LOW_WICK_MAX} | SAT ust <= %{HIGH_WICK_MAX}")
    log.info(f"  Zaman    : {TIMEFRAME}")
    log.info(f"  Aralik   : her {SCAN_INTERVAL} saniye")
    log.info(f"  Max coin : {MAX_COINS}")
    log.info(f"  Paralel  : {SCAN_WORKERS} worker, {REQUESTS_PER_SEC} istek/sn")
    log.info("=" * 55)

    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log.error("TELEGRAM_TOKEN ve TELEGRAM_CHAT_ID eksik!")
        return

    send_telegram(
        f"MAJOR KIRILIM BOT BASLADI\n"
        f"Strateji: SMA{MAJOR_LEN} kesisim + govde >= %{BODY_PCT_MIN}\n"
        f"Fitil: AL alt <= %{LOW_WICK_MAX} | SAT ust <= %{HIGH_WICK_MAX}\n"
        f"Zaman: {TIMEFRAME} | Aralik: {SCAN_INTERVAL//60} dk\n"
        f"Max coin: {MAX_COINS} | Paralel: {SCAN_WORKERS}\n"
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
