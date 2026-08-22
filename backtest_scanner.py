# -*- coding: utf-8 -*-
"""
15dk %7+ MUM + 1dk PULLBACK ONAYI - BACKTEST (CMO DUZELTMESI UYGULANMIS)

Canli scanner ile AYNI mantigi, gecmis Binance Futures verisi uzerinde
kosarak calistirir ve hit-rate / ortalama lehte hareket / gunluk sinyal
sayisi gibi istatistikleri uretir.

CMO DUZELTMESI: Onceki versiyonda CMO tek barlik ani harekete gore
+-100'e saturasyona ugruyordu (rolling sum eksikti). Bu yuzden "pullback
onayi" aslinda fiyat TEKRAR ziplarken tetikleniyordu, pullback'in
kendisinde degil. Burada CMO_PERIOD bar boyunca gercek toplama yapiliyor.

CALISTIRMA (Render shell):
    pip install requests pandas numpy
    python backtest_scanner.py

ORTAM DEGISKENLERI (hepsi opsiyonel, varsayilanlar test icin makul):
    SYMBOLS               - virgulle ayrilmis sembol listesi (bos ise
                             otomatik + likidite filtresiyle secilir)
                             ornek: "BTCUSDT,ETHUSDT,SOLUSDT"
    MAX_COINS             - otomatik secimde ust sinir (varsayilan 50)
    BACKTEST_DAYS         - kac gunluk gecmis (varsayilan 14)
    TARGET_PCT            - "hit" sayilmasi icin lehte hareket esigi (5.0)
    HORIZON_MINUTES        - onay sonrasi ne kadar ileri bakilacak (240)
    BODY_PCT_THRESHOLD    - 15dk govde esigi (7.0)
    PULLBACK_SEARCH_CANDLES - onay arama penceresi, 1dk mum sayisi (45)
    CMO_PERIOD             - CMO rolling toplama periyodu (9)
    MIN_QUOTE_VOLUME_24H   - likidite filtresi (3_000_000)
    MAX_WORKERS             - paralel fetch thread sayisi (4)
    CACHE_DIR               - indirilen mumlarin onbellek klasoru (./bt_cache)

CIKTI:
    ./bt_cache/ altinda ham kline onbellegi
    ./backtest_signals.csv  - her sinyalin detayi
    konsola ozet istatistik tablosu
"""
import os
import time
import logging
import hashlib
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed

import numpy as np
import pandas as pd
import requests

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════════════
# AYARLAR
# ══════════════════════════════════════════════════════════════════
SYMBOLS_OVERRIDE = os.getenv("SYMBOLS", "").strip()
MAX_COINS = int(os.getenv("MAX_COINS", "400"))
BACKTEST_DAYS = int(os.getenv("BACKTEST_DAYS", "15"))
TARGET_PCT = float(os.getenv("TARGET_PCT", "5.0"))
HORIZON_MINUTES = int(os.getenv("HORIZON_MINUTES", "240"))

EVENT_TF = "15m"
CONFIRM_TF = "1m"
BODY_PCT_THRESHOLD = float(os.getenv("BODY_PCT_THRESHOLD", "7.0"))
PULLBACK_SEARCH_CANDLES = int(os.getenv("PULLBACK_SEARCH_CANDLES", "40"))
WARMUP_MINUTES = int(os.getenv("WARMUP_MINUTES", "90"))

RSI_PERIOD = int(os.getenv("RSI_PERIOD", "9"))
CMO_HMA_FAST = int(os.getenv("CMO_HMA_FAST", "5"))
CMO_HMA_SLOW = int(os.getenv("CMO_HMA_SLOW", "12"))
CMO_PERIOD = int(os.getenv("CMO_PERIOD", "9"))            # <-- FIX
PIVOT_LEFT_RIGHT = int(os.getenv("PIVOT_LEFT_RIGHT", "2"))

USE_LIQUIDITY_FILTER = os.getenv("USE_LIQUIDITY_FILTER", "true").lower() == "true"
MIN_QUOTE_VOLUME_24H = float(os.getenv("MIN_QUOTE_VOLUME_24H", "3000000"))

MAX_WORKERS = int(os.getenv("MAX_WORKERS", "4"))
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "15"))
MAX_RETRIES = int(os.getenv("MAX_RETRIES", "3"))
RETRY_BACKOFF_BASE = float(os.getenv("RETRY_BACKOFF_BASE", "0.5"))
CACHE_DIR = os.getenv("CACHE_DIR", "./bt_cache")

BINANCE_BASE = "https://fapi.binance.com"

os.makedirs(CACHE_DIR, exist_ok=True)

session = requests.Session()
_adapter = requests.adapters.HTTPAdapter(pool_connections=MAX_WORKERS + 5, pool_maxsize=MAX_WORKERS + 10)
session.mount("https://", _adapter)


# ══════════════════════════════════════════════════════════════════
# HTTP + RETRY
# ══════════════════════════════════════════════════════════════════
def _request_with_retry(url, params=None):
    last_exc = None
    for attempt in range(MAX_RETRIES + 1):
        try:
            r = session.get(url, params=params, timeout=REQUEST_TIMEOUT)
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
                log.warning(f"HTTP {r.status_code}, {wait:.1f}sn sonra tekrar")
                time.sleep(wait)
                last_exc = Exception(f"HTTP {r.status_code}")
                continue
            r.raise_for_status()
        except requests.exceptions.RequestException as e:
            last_exc = e
            time.sleep(RETRY_BACKOFF_BASE * (2 ** attempt))
    if last_exc:
        raise last_exc
    raise Exception("Bilinmeyen istek hatasi")


# ══════════════════════════════════════════════════════════════════
# SEMBOL / LIKIDITE
# ══════════════════════════════════════════════════════════════════
def get_symbols():
    if SYMBOLS_OVERRIDE:
        return [s.strip().upper() for s in SYMBOLS_OVERRIDE.split(",") if s.strip()]
    r = _request_with_retry(f"{BINANCE_BASE}/fapi/v1/exchangeInfo")
    data = r.json()
    syms = [s["symbol"] for s in data["symbols"]
            if s["symbol"].endswith("USDT") and s["status"] == "TRADING"]
    if USE_LIQUIDITY_FILTER:
        vols = get_24h_volumes()
        syms = [s for s in syms if vols.get(s, 0.0) >= MIN_QUOTE_VOLUME_24H]
    return syms[:MAX_COINS]


def get_24h_volumes():
    # NOT: bu ANLIK 24s hacim - gecmisteki likiditenin yaklasik bir
    # tahmini olarak kullaniliyor, tarihsel olarak kesin degil.
    r = _request_with_retry(f"{BINANCE_BASE}/fapi/v1/ticker/24hr")
    data = r.json()
    return {d["symbol"]: float(d.get("quoteVolume", 0)) for d in data}


# ══════════════════════════════════════════════════════════════════
# GECMIS KLINE CEKME (sayfali + onbellekli)
# ══════════════════════════════════════════════════════════════════
def _cache_path(symbol, interval, start_ms, end_ms):
    key = f"{symbol}_{interval}_{start_ms}_{end_ms}"
    h = hashlib.md5(key.encode()).hexdigest()[:10]
    return os.path.join(CACHE_DIR, f"{symbol}_{interval}_{h}.csv")


def fetch_klines_range(symbol, interval, start_ms, end_ms):
    cache_file = _cache_path(symbol, interval, start_ms, end_ms)
    if os.path.exists(cache_file):
        try:
            return pd.read_csv(cache_file)
        except Exception:
            pass  # bozuk onbellek - yeniden indir

    all_rows = []
    cursor = start_ms
    interval_ms = {"1m": 60_000, "15m": 15 * 60_000}[interval]
    while cursor < end_ms:
        r = _request_with_retry(
            f"{BINANCE_BASE}/fapi/v1/klines",
            params={"symbol": symbol, "interval": interval, "startTime": cursor,
                    "endTime": end_ms, "limit": 1500},
        )
        raw = r.json()
        if not raw:
            break
        all_rows.extend(raw)
        last_open = int(raw[-1][0])
        next_cursor = last_open + interval_ms
        if next_cursor <= cursor:
            break
        cursor = next_cursor
        if len(raw) < 1500:
            break
        time.sleep(0.15)  # rate limit icin nazik ol

    if not all_rows:
        return None

    df = pd.DataFrame(all_rows, columns=[
        "open_time", "open", "high", "low", "close", "volume", "close_time",
        "qav", "trades", "taker_buy_base", "taker_buy_quote", "ignore"])
    for c in ["open", "high", "low", "close", "volume"]:
        df[c] = df[c].astype(float)
    df["open_time"] = df["open_time"].astype(np.int64)
    df["close_time"] = df["close_time"].astype(np.int64)
    df = df.drop_duplicates(subset="open_time").sort_values("open_time").reset_index(drop=True)
    df.to_csv(cache_file, index=False)
    return df


# ══════════════════════════════════════════════════════════════════
# INDIKATORLER (canli scanner ile birebir ayni, CMO duzeltmesi dahil)
# ══════════════════════════════════════════════════════════════════
def compute_rsi(close, period):
    delta = close.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = -delta.where(delta < 0, 0.0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def wma(series, length):
    weights = np.arange(1, length + 1)
    return series.rolling(length).apply(lambda x: np.dot(x, weights) / weights.sum(), raw=True)


def hma(series, length):
    half = max(1, int(length / 2))
    sqrt_len = max(1, int(round(np.sqrt(length))))
    wma_half = wma(series, half)
    wma_full = wma(series, length)
    diff = 2 * wma_half - wma_full
    return wma(diff, sqrt_len)


def causal_pivot_exists(high, low, left, right):
    window = left + right + 1
    roll_max = high.rolling(window, center=True).max()
    roll_min = low.rolling(window, center=True).min()
    confirmed_high = (high == roll_max).shift(right).fillna(False)
    confirmed_low = (low == roll_min).shift(right).fillna(False)
    return (confirmed_high.cumsum() > 0), (confirmed_low.cumsum() > 0)


def compute_sup_res(df_1m):
    close = df_1m["close"].astype(float)
    open_ = df_1m["open"].astype(float)
    high = df_1m["high"].astype(float)
    low = df_1m["low"].astype(float)

    rsi_new = compute_rsi(close, RSI_PERIOD)

    hma5_open = hma(open_, CMO_HMA_FAST).shift(1)
    hma12_close = hma(close, CMO_HMA_SLOW)
    momm1 = hma5_open.diff()
    momm2 = hma12_close.diff()

    m1 = pd.Series(np.where(momm1 >= momm2, momm1, 0.0), index=df_1m.index)
    m2 = pd.Series(np.where(momm1 >= momm2, 0.0, -momm1), index=df_1m.index)

    # >>> CMO FIX: periyot boyunca rolling sum <<<
    sm1 = m1.rolling(CMO_PERIOD, min_periods=CMO_PERIOD).sum()
    sm2 = m2.rolling(CMO_PERIOD, min_periods=CMO_PERIOD).sum()

    denom = sm1 + sm2
    with np.errstate(divide="ignore", invalid="ignore"):
        cmo_new = np.where(denom != 0, 100 * (sm1 - sm2) / denom, np.nan)
    cmo_new = pd.Series(cmo_new, index=df_1m.index)

    hpivot_exists, lpivot_exists = causal_pivot_exists(high, low, PIVOT_LEFT_RIGHT, PIVOT_LEFT_RIGHT)

    sup = (rsi_new < 25) & (cmo_new > 50) & lpivot_exists
    res = (rsi_new > 75) & (cmo_new < -50) & hpivot_exists
    return sup.fillna(False), res.fillna(False)


# ══════════════════════════════════════════════════════════════════
# BACKTEST MOTORU (canli scanner'daki "bir seferde tek bekleyen olay"
# kuralinin gecmis veri uzerinde birebir simulasyonu)
# ══════════════════════════════════════════════════════════════════
def backtest_symbol(symbol, df15, df1m, backtest_end_ms):
    sup, res = compute_sup_res(df1m)
    open_times_1m = df1m["open_time"].values

    signals = []
    pending_until_ms = -1

    for _, row in df15.iterrows():
        open_time = int(row["open_time"])
        close_time = int(row["close_time"])
        if open_time > backtest_end_ms:
            break
        if close_time <= pending_until_ms:
            continue  # bekleyen olay cozulmeden yeni olay yok (canliyla ayni)

        body_pct = (float(row["close"]) - float(row["open"])) / float(row["open"]) * 100
        direction = None
        if body_pct >= BODY_PCT_THRESHOLD:
            direction = "LONG"
        elif body_pct <= -BODY_PCT_THRESHOLD:
            direction = "SHORT"
        if direction is None:
            continue

        expire_ms = close_time + PULLBACK_SEARCH_CANDLES * 60_000
        mask = (open_times_1m >= close_time) & (open_times_1m < expire_ms)
        cond = (sup.values if direction == "LONG" else res.values) & mask
        hit_idx = np.flatnonzero(cond)

        if len(hit_idx) == 0:
            pending_until_ms = expire_ms  # onay gelmedi, olay iptal
            continue

        entry_idx = hit_idx[0]
        entry_time = int(open_times_1m[entry_idx])
        entry_price = float(df1m["close"].iloc[entry_idx])
        pending_until_ms = entry_time  # onaylandi, hemen serbest

        horizon_end = entry_time + HORIZON_MINUTES * 60_000
        fwd_mask = (open_times_1m >= entry_time) & (open_times_1m <= horizon_end)
        fwd = df1m[fwd_mask]
        if fwd.empty or len(fwd) < 2:
            continue

        if direction == "LONG":
            mfe = (fwd["high"].max() - entry_price) / entry_price * 100
            mae = (fwd["low"].min() - entry_price) / entry_price * 100
            running_gain = (fwd["high"] - entry_price) / entry_price * 100
            hit_mask = running_gain >= TARGET_PCT
        else:
            mfe = (entry_price - fwd["low"].min()) / entry_price * 100
            mae = (entry_price - fwd["high"].max()) / entry_price * 100
            running_gain = (entry_price - fwd["low"]) / entry_price * 100
            hit_mask = running_gain >= TARGET_PCT

        hit = bool(hit_mask.any())
        time_to_hit_min = None
        if hit:
            hit_time = int(fwd.loc[hit_mask, "open_time"].iloc[0])
            time_to_hit_min = (hit_time - entry_time) // 60_000

        minutes_to_confirm = int((entry_time - close_time) / 60_000)

        signals.append({
            "symbol": symbol,
            "direction": direction,
            "event_open_time": datetime.fromtimestamp(open_time / 1000, tz=timezone.utc),
            "event_body_pct": round(body_pct, 2),
            "entry_time": datetime.fromtimestamp(entry_time / 1000, tz=timezone.utc),
            "entry_price": entry_price,
            "minutes_to_confirm": minutes_to_confirm,
            "mfe_pct": round(mfe, 2),
            "mae_pct": round(mae, 2),
            "hit_target": hit,
            "time_to_hit_min": time_to_hit_min,
        })

    return signals


def process_symbol(symbol, start_ms, end_ms):
    fetch_start_1m = start_ms - WARMUP_MINUTES * 60_000
    fetch_end = end_ms + HORIZON_MINUTES * 60_000

    df15 = fetch_klines_range(symbol, EVENT_TF, start_ms, end_ms)
    df1m = fetch_klines_range(symbol, CONFIRM_TF, fetch_start_1m, fetch_end)

    if df15 is None or df1m is None or len(df15) < 2 or len(df1m) < WARMUP_MINUTES:
        return []

    try:
        return backtest_symbol(symbol, df15, df1m, end_ms)
    except Exception as e:
        log.warning(f"{symbol} backtest hatasi: {e}")
        return []


# ══════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════
def main():
    end_dt = datetime.now(tz=timezone.utc)
    start_dt = end_dt - timedelta(days=BACKTEST_DAYS)
    start_ms = int(start_dt.timestamp() * 1000)
    end_ms = int(end_dt.timestamp() * 1000)

    symbols = get_symbols()
    log.info("=" * 60)
    log.info(f"BACKTEST | {len(symbols)} sembol | {BACKTEST_DAYS} gun "
              f"({start_dt:%Y-%m-%d} -> {end_dt:%Y-%m-%d})")
    log.info(f"Govde esigi: %{BODY_PCT_THRESHOLD} | Onay penceresi: {PULLBACK_SEARCH_CANDLES} mum | "
              f"CMO_PERIOD: {CMO_PERIOD} | Hedef: %{TARGET_PCT} / {HORIZON_MINUTES}dk")
    log.info("=" * 60)

    all_signals = []
    done = 0
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(process_symbol, s, start_ms, end_ms): s for s in symbols}
        for future in as_completed(futures):
            sym = futures[future]
            done += 1
            try:
                sigs = future.result()
                all_signals.extend(sigs)
                if sigs:
                    log.info(f"[{done}/{len(symbols)}] {sym}: {len(sigs)} sinyal")
                elif done % 10 == 0:
                    log.info(f"[{done}/{len(symbols)}] tarandi...")
            except Exception as e:
                log.warning(f"{sym} hata: {e}")

    if not all_signals:
        log.info("Hic sinyal bulunamadi.")
        return

    df = pd.DataFrame(all_signals).sort_values("entry_time").reset_index(drop=True)
    df.to_csv("backtest_signals.csv", index=False)

    total = len(df)
    hit_rate = df["hit_target"].mean() * 100
    avg_mfe = df["mfe_pct"].mean()
    avg_mae = df["mae_pct"].mean()
    avg_per_day = total / BACKTEST_DAYS
    avg_confirm_min = df["minutes_to_confirm"].mean()
    avg_time_to_hit = df.loc[df["hit_target"], "time_to_hit_min"].mean()

    log.info("=" * 60)
    log.info("OZET SONUCLAR")
    log.info("=" * 60)
    log.info(f"Toplam sinyal            : {total}")
    log.info(f"Gunde ortalama sinyal    : {avg_per_day:.2f}")
    log.info(f"%{TARGET_PCT} hedefe ulasma orani (hit-rate) : %{hit_rate:.1f}")
    log.info(f"Ortalama lehte hareket (MFE)  : %{avg_mfe:.2f}")
    log.info(f"Ortalama aleyhte hareket (MAE): %{avg_mae:.2f}")
    log.info(f"Ort. onay suresi (event->entry): {avg_confirm_min:.1f} dk")
    if not np.isnan(avg_time_to_hit):
        log.info(f"Ort. hedefe ulasma suresi      : {avg_time_to_hit:.1f} dk")

    for d in ["LONG", "SHORT"]:
        sub = df[df["direction"] == d]
        if len(sub) == 0:
            continue
        log.info(f"  {d}: {len(sub)} sinyal | hit-rate %{sub['hit_target'].mean()*100:.1f} "
                  f"| avg MFE %{sub['mfe_pct'].mean():.2f}")

    log.info("=" * 60)
    log.info("Detaylar: backtest_signals.csv")


if __name__ == "__main__":
    main()
