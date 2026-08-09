# -*- coding: utf-8 -*-
"""
BIRLESIK BACKTEST: 15DK KIRILIM + 1DK MIKRO-GIRIS ANALIZI
=============================================================
Tek calistirmada:
  1) Son BACKTEST_DAYS gunde 15dk direnc kirilim sinyallerini bulur ve
     +%7/+%20 hedeflerini (direkt giris varsayimiyla) simule eder.
  2) Bulunan HER sinyal icin, kirilim mumu kapandiktan sonraki dakikalari
     1dk mumlarla tarayip "birkac kirmizi mum -> hacimli yesil donus"
     (mikro-giris) paternini arar, varsa o noktadan itibaren tekrar
     +%7/+%20 simulasyonu yapar.
  3) Iki yaklasimi (direkt giris vs mikro-giris) yan yana raporlar.

Ara CSV dosyasina ihtiyac YOK -> Render'da tek deploy, tek calistirma yeterli.

ONEMLI: Binance Futures API'sine (fapi.binance.com) canli ag gerektirir.
Kullanim:
    pip install requests pandas numpy
    python full_backtest_combined.py
"""
import os
import time
import logging
import threading
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
import pandas as pd
import numpy as np

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger(__name__)

BINANCE_BASE = "https://fapi.binance.com"

# ══════════════════════════════════════════════════════════════════
# AYARLAR
# ══════════════════════════════════════════════════════════════════
TIMEFRAME = "15m"
RES_LOOKBACK = int(os.getenv("RES_LOOKBACK", "50"))
RES_BREAK_PCT = float(os.getenv("RES_BREAK_PCT", "0.5"))
MIN_CANDLE_BODY_PCT = float(os.getenv("MIN_CANDLE_BODY_PCT", "10.0"))

USE_LIQUIDITY_FILTER = os.getenv("USE_LIQUIDITY_FILTER", "true").lower() == "true"
MIN_QUOTE_VOLUME_24H = float(os.getenv("MIN_QUOTE_VOLUME_24H", "5000000"))

BACKTEST_DAYS = int(os.getenv("BACKTEST_DAYS", "30"))
MAX_COINS = int(os.getenv("MAX_COINS", "1000"))   # 1000 = pratikte tum likit evren
MAX_WORKERS = int(os.getenv("MAX_WORKERS", "4"))
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "10"))
SYMBOLS_OVERRIDE = os.getenv("SYMBOLS_OVERRIDE", "")

TARGETS_PCT = [7.0, 20.0]
FORWARD_MAX_BARS = int(os.getenv("FORWARD_MAX_BARS", "192"))   # 48 saat (15dk cinsinden)
RETEST_LOOKAHEAD_BARS = int(os.getenv("RETEST_LOOKAHEAD_BARS", "20"))
RETEST_TOUCH_TOLERANCE_PCT = 0.3

# --- mikro-giris ayarlari ---
WATCH_WINDOW_MIN = int(os.getenv("WATCH_WINDOW_MIN", "20"))
MIN_RED_STREAK = int(os.getenv("MIN_RED_STREAK", "2"))
REVERSAL_VOL_MULT = float(os.getenv("REVERSAL_VOL_MULT", "1.3"))

OUTPUT_CSV = os.getenv("OUTPUT_CSV", "backtest_signals.csv")
MICRO_OUTPUT_CSV = os.getenv("MICRO_OUTPUT_CSV", "micro_entry_results.csv")
SUMMARY_CSV = os.getenv("SUMMARY_CSV", "backtest_summary.csv")

# --- global hiz sinirlama (429 onlemi) ---
REQUEST_MIN_INTERVAL = float(os.getenv("REQUEST_MIN_INTERVAL", "0.15"))
session = requests.Session()
_rate_lock = threading.Lock()
_last_request_time = [0.0]


def _throttle():
    with _rate_lock:
        now = time.time()
        wait = REQUEST_MIN_INTERVAL - (now - _last_request_time[0])
        if wait > 0:
            time.sleep(wait)
        _last_request_time[0] = time.time()


def _get(url, params=None, retries=5):
    for attempt in range(retries):
        _throttle()
        try:
            r = session.get(url, params=params, timeout=REQUEST_TIMEOUT)
            if r.status_code in (429, 418):
                retry_after = r.headers.get("Retry-After")
                wait = float(retry_after) if retry_after else 8 * (attempt + 1)
                log.warning(f"Rate limit ({r.status_code}), {wait:.0f}sn bekleniyor...")
                time.sleep(wait)
                continue
            r.raise_for_status()
            return r.json()
        except Exception as e:
            if attempt == retries - 1:
                log.error(f"Istek basarisiz ({url}): {e}")
                return None
            time.sleep(3 * (attempt + 1))
    return None


def get_symbols():
    if SYMBOLS_OVERRIDE.strip():
        return [s.strip().upper() for s in SYMBOLS_OVERRIDE.split(",") if s.strip()]
    data = _get(f"{BINANCE_BASE}/fapi/v1/exchangeInfo")
    if not data:
        return []
    syms = [s["symbol"] for s in data["symbols"]
            if s["symbol"].endswith("USDT") and s["status"] == "TRADING"]
    if USE_LIQUIDITY_FILTER:
        vols = get_all_24h_quote_volumes()
        syms = [s for s in syms if vols.get(s, 0) >= MIN_QUOTE_VOLUME_24H]
        syms.sort(key=lambda s: vols.get(s, 0), reverse=True)
    return syms[:MAX_COINS]


def get_all_24h_quote_volumes():
    data = _get(f"{BINANCE_BASE}/fapi/v1/ticker/24hr")
    if not data:
        return {}
    return {d["symbol"]: float(d.get("quoteVolume", 0)) for d in data}


def get_klines_range(symbol, interval, start_ms, end_ms, limit=1500):
    all_rows = []
    cur = start_ms
    guard = 0
    while cur < end_ms and guard < 50:
        guard += 1
        params = {"symbol": symbol, "interval": interval, "startTime": cur,
                   "endTime": end_ms, "limit": limit}
        raw = _get(f"{BINANCE_BASE}/fapi/v1/klines", params=params)
        if not raw:
            break
        all_rows.extend(raw)
        if len(raw) < limit:
            break
        cur = raw[-1][0] + 1
    if not all_rows:
        return None
    df = pd.DataFrame(all_rows, columns=[
        "open_time", "open", "high", "low", "close", "volume", "close_time",
        "qav", "trades", "tbv", "tqv", "ignore"])
    for c in ["open", "high", "low", "close", "volume"]:
        df[c] = df[c].astype(float)
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    return df.drop_duplicates(subset="open_time").reset_index(drop=True)


# ══════════════════════════════════════════════════════════════════
# INDIKATORLER
# ══════════════════════════════════════════════════════════════════
def compute_rsi(close: pd.Series, period=14):
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    return rsi.fillna(50)


def compute_atr(df: pd.DataFrame, period=14):
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low, (high - prev_close).abs(), (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()


def compute_ema(series: pd.Series, period):
    return series.ewm(span=period, adjust=False).mean()


# ══════════════════════════════════════════════════════════════════
# 15DK SINYAL TESPITI + DIREKT GIRIS SIMULASYONU
# ══════════════════════════════════════════════════════════════════
@dataclass
class SignalRecord:
    symbol: str
    signal_time: object          # 15dk kirilim mumunun ACILIS zamani (pd.Timestamp)
    entry_price: float
    resistance: float
    break_pct: float
    body_pct: float
    volume_ratio: float
    rsi14: float
    atr_body_ratio: float
    trend_1h_up: object
    retest_occurred: bool
    retest_held: bool
    retest_bars: object
    outcome_7pct: str
    bars_to_7pct: object
    outcome_20pct: str
    bars_to_20pct: object
    bars_to_stop: object


def find_signals_and_evaluate(symbol, df15, df1h):
    if df15 is None or len(df15) < RES_LOOKBACK + FORWARD_MAX_BARS + 5:
        return []

    df15 = df15.copy()
    df15["rsi14"] = compute_rsi(df15["close"], 14)
    df15["atr14"] = compute_atr(df15, 14)
    df15["vol_ma20"] = df15["volume"].rolling(20).mean()

    trend_up_series = None
    if df1h is not None and len(df1h) > 210:
        ema50_1h = compute_ema(df1h["close"], 50)
        ema200_1h = compute_ema(df1h["close"], 200)
        trend_up_series = (ema50_1h > ema200_1h)
        trend_up_series.index = df1h["open_time"]

    results = []
    n = len(df15)
    scan_end = n - FORWARD_MAX_BARS - 1
    for i in range(RES_LOOKBACK, scan_end):
        history = df15.iloc[i - RES_LOOKBACK:i]
        breakout = df15.iloc[i]

        resistance = float(history["high"].max())
        open_now = float(breakout["open"])
        close_now = float(breakout["close"])

        if close_now <= open_now:
            continue
        if close_now <= resistance:
            continue

        break_pct = (close_now - resistance) / resistance * 100
        body_pct = (close_now - open_now) / open_now * 100
        if break_pct < RES_BREAK_PCT or body_pct < MIN_CANDLE_BODY_PCT:
            continue

        vol_ma20 = breakout["vol_ma20"]
        volume_ratio = float(breakout["volume"] / vol_ma20) if vol_ma20 and vol_ma20 > 0 else np.nan
        rsi_val = float(breakout["rsi14"])
        atr_val = float(breakout["atr14"]) if not np.isnan(breakout["atr14"]) else np.nan
        atr_body_ratio = float((close_now - open_now) / atr_val) if atr_val and atr_val > 0 else np.nan

        trend_up = None
        if trend_up_series is not None:
            past_hours = trend_up_series[trend_up_series.index <= breakout["open_time"]]
            if len(past_hours) > 0:
                trend_up = bool(past_hours.iloc[-1])

        forward = df15.iloc[i + 1: i + 1 + FORWARD_MAX_BARS]
        entry = close_now
        targets = {t: entry * (1 + t / 100) for t in TARGETS_PCT}
        outcome = {t: None for t in TARGETS_PCT}
        bars_to = {t: None for t in TARGETS_PCT}
        bars_to_stop = None

        for j, row in enumerate(forward.itertuples(index=False), start=1):
            stop_this_bar = row.low <= resistance
            if stop_this_bar and bars_to_stop is None:
                bars_to_stop = j
            for t in TARGETS_PCT:
                if outcome[t] is not None:
                    continue
                if row.high >= targets[t]:
                    if stop_this_bar or (bars_to_stop is not None and bars_to_stop <= j):
                        outcome[t] = "STOP"
                    else:
                        outcome[t] = "SUCCESS"
                        bars_to[t] = j
                elif bars_to_stop is not None and bars_to_stop <= j:
                    outcome[t] = "STOP"
            if all(outcome[t] is not None for t in TARGETS_PCT):
                break
        for t in TARGETS_PCT:
            if outcome[t] is None:
                outcome[t] = "TIMEOUT"

        retest_occurred = False
        retest_held = False
        retest_bar = None
        lookahead = df15.iloc[i + 1: i + 1 + RETEST_LOOKAHEAD_BARS]
        for j, row in enumerate(lookahead.itertuples(index=False), start=1):
            if row.low <= resistance * (1 + RETEST_TOUCH_TOLERANCE_PCT / 100):
                retest_occurred = True
                retest_bar = j
                retest_held = row.close >= resistance
                break

        results.append(SignalRecord(
            symbol=symbol, signal_time=breakout["open_time"], entry_price=entry,
            resistance=resistance, break_pct=break_pct, body_pct=body_pct,
            volume_ratio=volume_ratio, rsi14=rsi_val, atr_body_ratio=atr_body_ratio,
            trend_1h_up=trend_up, retest_occurred=retest_occurred, retest_held=retest_held,
            retest_bars=retest_bar, outcome_7pct=outcome[7.0], bars_to_7pct=bars_to[7.0],
            outcome_20pct=outcome[20.0], bars_to_20pct=bars_to[20.0], bars_to_stop=bars_to_stop,
        ))
    return results


def process_symbol(symbol, start_ms, end_ms, buffer_ms):
    try:
        df15 = get_klines_range(symbol, "15m", start_ms - buffer_ms, end_ms)
        df1h = get_klines_range(symbol, "1h", start_ms - buffer_ms, end_ms)
        if df15 is None:
            return symbol, []
        return symbol, find_signals_and_evaluate(symbol, df15, df1h)
    except Exception as e:
        log.error(f"{symbol} islenirken hata: {e}")
        return symbol, []


# ══════════════════════════════════════════════════════════════════
# 1DK MIKRO-GIRIS ANALIZI
# ══════════════════════════════════════════════════════════════════
@dataclass
class MicroResult:
    symbol: str
    signal_time: str
    pattern_found: bool
    invalidated_before_entry: bool
    entry_time_new: object
    entry_price_new: object
    minutes_to_entry: object
    price_improvement_pct: object
    outcome_7pct_new: str
    outcome_20pct_new: str
    outcome_7pct_orig: str
    outcome_20pct_orig: str


def find_micro_entry(symbol, breakout_close_time, resistance):
    start_ms = int(breakout_close_time.timestamp() * 1000)
    end_ms = int((breakout_close_time + timedelta(minutes=WATCH_WINDOW_MIN)).timestamp() * 1000)
    df1m = get_klines_range(symbol, "1m", start_ms, end_ms)
    if df1m is None or len(df1m) < MIN_RED_STREAK + 1:
        return None

    red_streak = 0
    dip_vols = []
    last_red_high = None
    invalidated = False

    for _, row in df1m.iterrows():
        if row["low"] <= resistance:
            invalidated = True
            break
        is_red = row["close"] < row["open"]
        if is_red:
            red_streak += 1
            dip_vols.append(row["volume"])
            last_red_high = row["high"]
        elif red_streak >= MIN_RED_STREAK and not is_red:
            avg_dip_vol = np.mean(dip_vols) if dip_vols else 0
            vol_ok = avg_dip_vol > 0 and row["volume"] >= REVERSAL_VOL_MULT * avg_dip_vol
            reclaim_ok = last_red_high is not None and row["close"] > last_red_high
            if vol_ok and reclaim_ok:
                return {"entry_time": row["open_time"], "entry_price": float(row["close"]), "invalidated": False}
            else:
                red_streak = 0
                dip_vols = []
                last_red_high = None
        else:
            red_streak = 0
            dip_vols = []
            last_red_high = None

    if invalidated:
        return {"entry_time": None, "entry_price": None, "invalidated": True}
    return None


def simulate_forward_15m(symbol, entry_time, entry_price, resistance):
    start_ms = int(entry_time.timestamp() * 1000)
    end_ms = int((entry_time + timedelta(minutes=15 * FORWARD_MAX_BARS)).timestamp() * 1000)
    df15 = get_klines_range(symbol, "15m", start_ms, end_ms)
    if df15 is None or len(df15) == 0:
        return {7.0: "TIMEOUT", 20.0: "TIMEOUT"}

    targets = {t: entry_price * (1 + t / 100) for t in TARGETS_PCT}
    outcome = {t: None for t in TARGETS_PCT}
    bars_to_stop = None
    for j, row in enumerate(df15.itertuples(index=False), start=1):
        stop_this_bar = row.low <= resistance
        if stop_this_bar and bars_to_stop is None:
            bars_to_stop = j
        for t in TARGETS_PCT:
            if outcome[t] is not None:
                continue
            if row.high >= targets[t]:
                if stop_this_bar or (bars_to_stop is not None and bars_to_stop <= j):
                    outcome[t] = "STOP"
                else:
                    outcome[t] = "SUCCESS"
            elif bars_to_stop is not None and bars_to_stop <= j:
                outcome[t] = "STOP"
        if all(outcome[t] is not None for t in TARGETS_PCT):
            break
    for t in TARGETS_PCT:
        if outcome[t] is None:
            outcome[t] = "TIMEOUT"
    return outcome


def run_micro_entry_analysis(signal_records):
    results = []
    for idx, sig in enumerate(signal_records):
        symbol = sig.symbol
        breakout_close_time = sig.signal_time + timedelta(minutes=15)
        resistance = float(sig.resistance)

        found = find_micro_entry(symbol, breakout_close_time, resistance)

        if found is None:
            results.append(MicroResult(
                symbol=symbol, signal_time=str(sig.signal_time), pattern_found=False,
                invalidated_before_entry=False, entry_time_new=None, entry_price_new=None,
                minutes_to_entry=None, price_improvement_pct=None,
                outcome_7pct_new="NO_PATTERN", outcome_20pct_new="NO_PATTERN",
                outcome_7pct_orig=sig.outcome_7pct, outcome_20pct_orig=sig.outcome_20pct,
            ))
        elif found["invalidated"]:
            results.append(MicroResult(
                symbol=symbol, signal_time=str(sig.signal_time), pattern_found=False,
                invalidated_before_entry=True, entry_time_new=None, entry_price_new=None,
                minutes_to_entry=None, price_improvement_pct=None,
                outcome_7pct_new="INVALIDATED", outcome_20pct_new="INVALIDATED",
                outcome_7pct_orig=sig.outcome_7pct, outcome_20pct_orig=sig.outcome_20pct,
            ))
        else:
            entry_time_new = found["entry_time"]
            entry_price_new = found["entry_price"]
            minutes_to_entry = (entry_time_new - breakout_close_time).total_seconds() / 60
            price_improvement_pct = (entry_price_new - sig.entry_price) / sig.entry_price * 100
            outcome_new = simulate_forward_15m(symbol, entry_time_new, entry_price_new, resistance)
            results.append(MicroResult(
                symbol=symbol, signal_time=str(sig.signal_time), pattern_found=True,
                invalidated_before_entry=False, entry_time_new=str(entry_time_new),
                entry_price_new=entry_price_new, minutes_to_entry=minutes_to_entry,
                price_improvement_pct=price_improvement_pct,
                outcome_7pct_new=outcome_new[7.0], outcome_20pct_new=outcome_new[20.0],
                outcome_7pct_orig=sig.outcome_7pct, outcome_20pct_orig=sig.outcome_20pct,
            ))

        if (idx + 1) % 10 == 0 or (idx + 1) == len(signal_records):
            log.info(f"[mikro-giris {idx+1}/{len(signal_records)}] islendi")

    return results


# ══════════════════════════════════════════════════════════════════
# OZET (15dk direkt giris icin, oncekiyle ayni)
# ══════════════════════════════════════════════════════════════════
def build_summary(df: pd.DataFrame):
    rows = []

    def add_row(label, subset):
        n = len(subset)
        if n == 0:
            return
        succ7 = (subset["outcome_7pct"] == "SUCCESS").mean() * 100
        succ20 = (subset["outcome_20pct"] == "SUCCESS").mean() * 100
        rows.append({"kirilim": label, "sinyal_sayisi": n,
                     "basari_%7": round(succ7, 1), "basari_%20": round(succ20, 1)})

    add_row("TUMU (filtresiz)", df)
    for lo, hi, lbl in [(0, 1.5, "hacim_orani < 1.5x"), (1.5, 2.0, "hacim_orani 1.5x-2x"),
                         (2.0, 1e9, "hacim_orani > 2x")]:
        add_row(lbl, df[(df["volume_ratio"] >= lo) & (df["volume_ratio"] < hi)])
    for lo, hi, lbl in [(0, 60, "RSI < 60"), (60, 70, "RSI 60-70"), (70, 100, "RSI > 70")]:
        add_row(lbl, df[(df["rsi14"] >= lo) & (df["rsi14"] < hi)])
    if "trend_1h_up" in df.columns:
        add_row("1s trend YUKARI ile uyumlu", df[df["trend_1h_up"] == True])   # noqa: E712
        add_row("1s trend AŞAĞI (uyumsuz)", df[df["trend_1h_up"] == False])  # noqa: E712
    add_row("retest OLUSTU ve TUTTU", df[(df["retest_occurred"] == True) & (df["retest_held"] == True)])   # noqa: E712
    add_row("retest OLUSTU ama TUTMADI", df[(df["retest_occurred"] == True) & (df["retest_held"] == False)])  # noqa: E712
    add_row("retest OLUSMADI (direkt devam)", df[df["retest_occurred"] == False])  # noqa: E712
    for wb in [2, 3, 4, 5]:
        survived = df[(df["bars_to_stop"].isna()) | (df["bars_to_stop"] > wb)]
        add_row(f"GECIKMELI GIRIS: {wb} mum bekle", survived)
    return pd.DataFrame(rows)


# ══════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════
def main():
    end_dt = datetime.now(timezone.utc)
    start_dt = end_dt - timedelta(days=BACKTEST_DAYS)
    end_ms = int(end_dt.timestamp() * 1000)
    start_ms = int(start_dt.timestamp() * 1000)
    buffer_ms = (RES_LOOKBACK + FORWARD_MAX_BARS + 10) * 15 * 60 * 1000

    log.info("=" * 60)
    log.info("BIRLESIK BACKTEST: 15DK KIRILIM + 1DK MIKRO-GIRIS")
    log.info(f"Aralik: {start_dt.date()} -> {end_dt.date()} ({BACKTEST_DAYS} gun) | Max coin: {MAX_COINS}")
    log.info("=" * 60)

    symbols = get_symbols()
    if not symbols:
        log.error("Sembol listesi bos, cikiliyor.")
        return
    log.info(f"{len(symbols)} coin taranacak (ASAMA 1: 15dk sinyal tespiti).")

    all_records = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(process_symbol, s, start_ms, end_ms, buffer_ms): s for s in symbols}
        done = 0
        for future in as_completed(futures):
            done += 1
            symbol, recs = future.result()
            all_records.extend(recs)
            if done % 20 == 0 or done == len(symbols):
                log.info(f"[ASAMA 1 - {done}/{len(symbols)}] islendi | toplam sinyal: {len(all_records)}")

    if not all_records:
        log.warning("Hic sinyal bulunamadi. Ayarlari veya tarihi kontrol et.")
        return

    df = pd.DataFrame([asdict(r) for r in all_records])
    df.to_csv(OUTPUT_CSV, index=False)
    log.info(f"ASAMA 1 tamamlandi: {len(df)} sinyal. Detay CSV: {OUTPUT_CSV}")

    summary = build_summary(df)
    summary.to_csv(SUMMARY_CSV, index=False)

    print("\n" + "=" * 70)
    print("ASAMA 1 OZET: 15DK KIRILIM (DIREKT GIRIS)")
    print("=" * 70)
    print(summary.to_string(index=False))
    overall7 = (df["outcome_7pct"] == "SUCCESS").mean() * 100
    overall20 = (df["outcome_20pct"] == "SUCCESS").mean() * 100
    print(f"\nToplam sinyal: {len(df)} | Filtresiz basari +%7: {overall7:.1f}% | +%20: {overall20:.1f}%")

    # --- ASAMA 2: mikro-giris analizi ---
    log.info(f"\nASAMA 2: {len(all_records)} sinyal icin 1dk mikro-giris analizi basliyor...")
    log.info(f"Ayarlar: watch={WATCH_WINDOW_MIN}dk min_red_streak={MIN_RED_STREAK} vol_mult={REVERSAL_VOL_MULT}")

    micro_results = run_micro_entry_analysis(all_records)
    mdf = pd.DataFrame([asdict(r) for r in micro_results])
    mdf.to_csv(MICRO_OUTPUT_CSV, index=False)
    log.info(f"ASAMA 2 tamamlandi. Detay CSV: {MICRO_OUTPUT_CSV}")

    n = len(mdf)
    n_pattern = (mdf["pattern_found"] == True).sum()          # noqa: E712
    n_invalid = (mdf["invalidated_before_entry"] == True).sum()  # noqa: E712
    n_timeout = n - n_pattern - n_invalid

    print("\n" + "=" * 70)
    print("ASAMA 2 OZET: 1DK MIKRO-GIRIS (DIP + DONUS TEYIDI)")
    print("=" * 70)
    print(f"Toplam sinyal: {n}")
    print(f"Pattern olustu: {n_pattern} (%{n_pattern/n*100:.1f})")
    print(f"Gecersiz oldu (giris oncesi stop): {n_invalid} (%{n_invalid/n*100:.1f})")
    print(f"Pattern hic olusmadi (timeout): {n_timeout} (%{n_timeout/n*100:.1f})")

    pattern_df = mdf[mdf["pattern_found"] == True]  # noqa: E712
    if len(pattern_df) > 0:
        succ7_new = (pattern_df["outcome_7pct_new"] == "SUCCESS").mean() * 100
        succ20_new = (pattern_df["outcome_20pct_new"] == "SUCCESS").mean() * 100
        avg_improve = pattern_df["price_improvement_pct"].mean()
        avg_minutes = pattern_df["minutes_to_entry"].mean()
        succ7_orig = (pattern_df["outcome_7pct_orig"] == "SUCCESS").mean() * 100
        succ20_orig = (pattern_df["outcome_20pct_orig"] == "SUCCESS").mean() * 100
        print(f"\n--- Pattern olusan sinyallerde (n={len(pattern_df)}) ---")
        print(f"YENI (mikro) giris basari: +%7={succ7_new:.1f}%  +%20={succ20_new:.1f}%")
        print(f"ORIJINAL (direkt) giris basari (ayni alt kume): +%7={succ7_orig:.1f}%  +%20={succ20_orig:.1f}%")
        print(f"Ortalama fiyat farki: %{avg_improve:.2f} (negatif=daha ucuz giris)")
        print(f"Ortalama giris gecikmesi: {avg_minutes:.1f} dakika")
    print("=" * 70)

    print("\n" + "=" * 70)
    print(f"TAM DETAY - 15DK SINYALLER CSV ICERIGI ({OUTPUT_CSV}):")
    print("=" * 70)
    print(df.to_csv(index=False))

    print("\n" + "=" * 70)
    print(f"TAM DETAY - MIKRO-GIRIS CSV ICERIGI ({MICRO_OUTPUT_CSV}):")
    print("=" * 70)
    print(mdf.to_csv(index=False))

    log.info("Tamamlandi. Sonsuz bekleme moduna geciliyor (Render restart-loop onlemi).")
    log.info("Sonuclari Logs sekmesinden kopyaladiktan sonra servisi SUSPEND edip eski botu geri koy.")
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    main()
