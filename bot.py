# -*- coding: utf-8 -*-
"""
DIRENC KIRILIM SCANNER - GERIYE DONUK BACKTEST
================================================
Amac: Mevcut "15DK DIRENC KIRILIM SCANNER" botunun urettigi sinyalleri
gecmis veride yeniden uretip, her sinyal icin:
  - +%7 hedefine ulasti mi (stop'tan once)?
  - +%20 hedefine ulasti mi (stop'tan once)?
  - Ek metrikler: hacim orani, RSI(14), 1s trend yonu, ATR/govde orani,
    retest olustu mu ve retest tuttu mu?
hesaplayip CSV'ye yazar. Boylece "hangi filtre basari oranini artiriyor"
sorusuna veriyle cevap verebiliriz.

ONEMLI: Bu script Binance Futures API'sine (fapi.binance.com) canli agi
gerektirir. Kendi sunucunda / botun calistigi ortamda calistir.

Kullanim:
    pip install requests pandas numpy
    python backtest_direnc_kirilim.py

Cikti:
    backtest_signals.csv   -> her sinyalin tum detaylari + sonuc
    backtest_summary.csv   -> filtre kirilimlarina gore basari oranlari
"""
import os
import sys
import time
import logging
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
import pandas as pd
import numpy as np

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════════════
# AYARLAR (sinyal mantigi botla BIREBIR ayni tutuldu)
# ══════════════════════════════════════════════════════════════════
BINANCE_BASE = "https://fapi.binance.com"

TIMEFRAME = "15m"
RES_LOOKBACK = int(os.getenv("RES_LOOKBACK", "50"))
RES_BREAK_PCT = float(os.getenv("RES_BREAK_PCT", "0.5"))
MIN_CANDLE_BODY_PCT = float(os.getenv("MIN_CANDLE_BODY_PCT", "10.0"))

USE_LIQUIDITY_FILTER = os.getenv("USE_LIQUIDITY_FILTER", "true").lower() == "true"
MIN_QUOTE_VOLUME_24H = float(os.getenv("MIN_QUOTE_VOLUME_24H", "5000000"))

# --- BACKTEST AYARLARI ---
BACKTEST_DAYS = int(os.getenv("BACKTEST_DAYS", "30"))
MAX_COINS = int(os.getenv("MAX_COINS", "150"))          # once kucuk basla, sonra artir
MAX_WORKERS = int(os.getenv("MAX_WORKERS", "8"))
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "10"))
SYMBOLS_OVERRIDE = os.getenv("SYMBOLS_OVERRIDE", "")     # "BTCUSDT,ETHUSDT" gibi, bos ise otomatik secilir

# Sonuc degerlendirme
TARGETS_PCT = [7.0, 20.0]
FORWARD_MAX_BARS = int(os.getenv("FORWARD_MAX_BARS", "192"))   # 192*15dk = 48 saat
RETEST_LOOKAHEAD_BARS = int(os.getenv("RETEST_LOOKAHEAD_BARS", "20"))
RETEST_TOUCH_TOLERANCE_PCT = 0.3   # direncin %0.3 yakinina dokunma = retest sayilir

OUTPUT_CSV = os.getenv("OUTPUT_CSV", "backtest_signals.csv")
SUMMARY_CSV = os.getenv("SUMMARY_CSV", "backtest_summary.csv")

session = requests.Session()


# ══════════════════════════════════════════════════════════════════
# BINANCE VERI CEKME (pagination + basit rate-limit koruma)
# ══════════════════════════════════════════════════════════════════
def _get(url, params=None, retries=3):
    for attempt in range(retries):
        try:
            r = session.get(url, params=params, timeout=REQUEST_TIMEOUT)
            if r.status_code == 429 or r.status_code == 418:
                wait = 5 * (attempt + 1)
                log.warning(f"Rate limit ({r.status_code}), {wait}sn bekleniyor...")
                time.sleep(wait)
                continue
            r.raise_for_status()
            return r.json()
        except Exception as e:
            if attempt == retries - 1:
                log.error(f"Istek basarisiz ({url}): {e}")
                return None
            time.sleep(2 * (attempt + 1))
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
        # en yuksek hacimliden basla (backtest'i temsili ve verimli tutmak icin)
        syms.sort(key=lambda s: vols.get(s, 0), reverse=True)

    return syms[:MAX_COINS]


def get_all_24h_quote_volumes():
    data = _get(f"{BINANCE_BASE}/fapi/v1/ticker/24hr")
    if not data:
        return {}
    return {d["symbol"]: float(d.get("quoteVolume", 0)) for d in data}


def get_klines_range(symbol, interval, start_ms, end_ms, limit=1500):
    """Belirtilen araligi (start_ms -> end_ms) sayfalayarak ceker."""
    all_rows = []
    cur = start_ms
    guard = 0
    while cur < end_ms and guard < 50:  # guard: sonsuz donguye karsi
        guard += 1
        params = {"symbol": symbol, "interval": interval, "startTime": cur,
                   "endTime": end_ms, "limit": limit}
        raw = _get(f"{BINANCE_BASE}/fapi/v1/klines", params=params)
        if not raw:
            break
        all_rows.extend(raw)
        if len(raw) < limit:
            break
        cur = raw[-1][0] + 1  # son mumun acilis zamanindan 1ms sonrasi
        time.sleep(0.05)  # nazik ol, rate limit yeme

    if not all_rows:
        return None

    df = pd.DataFrame(all_rows, columns=[
        "open_time", "open", "high", "low", "close", "volume", "close_time",
        "qav", "trades", "tbv", "tqv", "ignore"])
    for c in ["open", "high", "low", "close", "volume"]:
        df[c] = df[c].astype(float)
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    df = df.drop_duplicates(subset="open_time").reset_index(drop=True)
    return df


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
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()


def compute_ema(series: pd.Series, period):
    return series.ewm(span=period, adjust=False).mean()


# ══════════════════════════════════════════════════════════════════
# SINYAL TESPITI (bot ile ayni mantik, tum gecmis uzerinde tarama)
# ══════════════════════════════════════════════════════════════════
@dataclass
class SignalRecord:
    symbol: str
    signal_time: str
    entry_price: float
    resistance: float
    break_pct: float
    body_pct: float
    volume_ratio: float
    rsi14: float
    atr_body_ratio: float
    trend_1h_up: bool
    retest_occurred: bool
    retest_held: bool
    retest_bars: object
    outcome_7pct: str
    bars_to_7pct: object
    outcome_20pct: str
    bars_to_20pct: object
    bars_to_stop: object


def find_signals_and_evaluate(symbol, df15, df1h):
    """df15: 15dk mum verisi (tum backtest penceresi + RES_LOOKBACK icin ekstra pay).
    df1h: 1s mum verisi (trend icin)."""
    if df15 is None or len(df15) < RES_LOOKBACK + FORWARD_MAX_BARS + 5:
        return []

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
    # Son FORWARD_MAX_BARS mum icin ileri simulasyon yapamayacagimizdan
    # taramayi n - FORWARD_MAX_BARS - 1 ile sinirliyoruz.
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

        # --- ek metrikler ---
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

        # --- ileri simulasyon: +7% / +20% hedef ve stop (kirilan direncin alti) ---
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
                    # ayni bar icinde stop da tetiklendiyse, MUHAFAZAKAR varsayimla
                    # (intrabar sirasi OHLC'den kesin bilinemez) STOP kazanir.
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

        # --- retest tespiti ---
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
            symbol=symbol,
            signal_time=str(breakout["open_time"]),
            entry_price=entry,
            resistance=resistance,
            break_pct=break_pct,
            body_pct=body_pct,
            volume_ratio=volume_ratio,
            rsi14=rsi_val,
            atr_body_ratio=atr_body_ratio,
            trend_1h_up=trend_up,
            retest_occurred=retest_occurred,
            retest_held=retest_held,
            retest_bars=retest_bar,
            outcome_7pct=outcome[7.0],
            bars_to_7pct=bars_to[7.0],
            outcome_20pct=outcome[20.0],
            bars_to_20pct=bars_to[20.0],
            bars_to_stop=bars_to_stop,
        ))

    return results


# ══════════════════════════════════════════════════════════════════
# COIN BASINA ISLEM
# ══════════════════════════════════════════════════════════════════
def process_symbol(symbol, start_ms, end_ms, buffer_ms):
    try:
        df15 = get_klines_range(symbol, "15m", start_ms - buffer_ms, end_ms)
        df1h = get_klines_range(symbol, "1h", start_ms - buffer_ms, end_ms)
        if df15 is None:
            return symbol, []
        recs = find_signals_and_evaluate(symbol, df15, df1h)
        return symbol, recs
    except Exception as e:
        log.error(f"{symbol} islenirken hata: {e}")
        return symbol, []


# ══════════════════════════════════════════════════════════════════
# OZET RAPOR
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

    for lo, hi, lbl in [(0, 1.5, "hacim_orani < 1.5x"),
                         (1.5, 2.0, "hacim_orani 1.5x-2x"),
                         (2.0, 1e9, "hacim_orani > 2x")]:
        add_row(lbl, df[(df["volume_ratio"] >= lo) & (df["volume_ratio"] < hi)])

    for lo, hi, lbl in [(0, 60, "RSI < 60"), (60, 70, "RSI 60-70"),
                         (70, 100, "RSI > 70")]:
        add_row(lbl, df[(df["rsi14"] >= lo) & (df["rsi14"] < hi)])

    if "trend_1h_up" in df.columns:
        add_row("1s trend YUKARI ile uyumlu", df[df["trend_1h_up"] == True])   # noqa: E712
        add_row("1s trend AŞAĞI (uyumsuz)", df[df["trend_1h_up"] == False])  # noqa: E712

    add_row("retest OLUSTU ve TUTTU", df[(df["retest_occurred"] == True) & (df["retest_held"] == True)])   # noqa: E712
    add_row("retest OLUSTU ama TUTMADI", df[(df["retest_occurred"] == True) & (df["retest_held"] == False)])  # noqa: E712
    add_row("retest OLUSMADI (direkt devam)", df[df["retest_occurred"] == False])  # noqa: E712

    # en umut vaat eden kombinasyon ornegi
    combo = df[(df["volume_ratio"] >= 1.5) & (df["rsi14"] < 70)]
    add_row("KOMBO: hacim>=1.5x VE RSI<70", combo)

    combo2 = df[(df["volume_ratio"] >= 1.5) & (df["rsi14"] < 70) &
                (df["retest_occurred"] == True) & (df["retest_held"] == True)]  # noqa: E712
    add_row("KOMBO: hacim>=1.5x VE RSI<70 VE retest+tuttu", combo2)

    return pd.DataFrame(rows)


# ══════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════
def main():
    end_dt = datetime.now(timezone.utc)
    start_dt = end_dt - timedelta(days=BACKTEST_DAYS)
    end_ms = int(end_dt.timestamp() * 1000)
    start_ms = int(start_dt.timestamp() * 1000)
    # RES_LOOKBACK + FORWARD_MAX_BARS icin ekstra veri payi (15dk cinsinden -> ms)
    buffer_ms = (RES_LOOKBACK + FORWARD_MAX_BARS + 10) * 15 * 60 * 1000

    log.info("=" * 60)
    log.info("DIRENC KIRILIM - GERIYE DONUK BACKTEST")
    log.info(f"Aralik        : {start_dt.date()} -> {end_dt.date()} ({BACKTEST_DAYS} gun)")
    log.info(f"Max coin      : {MAX_COINS}")
    log.info(f"Sinyal ayari  : lookback={RES_LOOKBACK} break%={RES_BREAK_PCT} govde%={MIN_CANDLE_BODY_PCT}")
    log.info(f"Hedefler      : {TARGETS_PCT}  | Stop: kirilan direncin alti")
    log.info(f"Ileri pencere : {FORWARD_MAX_BARS} mum (~{FORWARD_MAX_BARS * 15 / 60:.0f} saat)")
    log.info("=" * 60)

    symbols = get_symbols()
    if not symbols:
        log.error("Sembol listesi bos, cikiliyor.")
        return
    log.info(f"{len(symbols)} coin taranacak.")

    all_records = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(process_symbol, s, start_ms, end_ms, buffer_ms): s
                   for s in symbols}
        done = 0
        for future in as_completed(futures):
            done += 1
            symbol, recs = future.result()
            all_records.extend(recs)
            if done % 10 == 0 or done == len(symbols):
                log.info(f"[{done}/{len(symbols)}] islendi | toplam sinyal: {len(all_records)}")

    if not all_records:
        log.warning("Hic sinyal bulunamadi. Ayarlari veya tarihi kontrol et.")
        return

    df = pd.DataFrame([asdict(r) for r in all_records])
    df.to_csv(OUTPUT_CSV, index=False)
    log.info(f"Detay CSV yazildi: {OUTPUT_CSV} ({len(df)} sinyal)")

    summary = build_summary(df)
    summary.to_csv(SUMMARY_CSV, index=False)
    log.info(f"Ozet CSV yazildi: {SUMMARY_CSV}")

    print("\n" + "=" * 70)
    print("OZET SONUCLAR")
    print("=" * 70)
    print(summary.to_string(index=False))
    print("=" * 70)
    overall7 = (df["outcome_7pct"] == "SUCCESS").mean() * 100
    overall20 = (df["outcome_20pct"] == "SUCCESS").mean() * 100
    print(f"\nToplam sinyal: {len(df)}")
    print(f"Filtresiz genel basari (+%7): {overall7:.1f}%")
    print(f"Filtresiz genel basari (+%20): {overall20:.1f}%")
    print("\nBu oranlari senin gercek 'canli' 3-4/14 (~%21-29) oranınla karsilastir.")
    print("Sonra summary.csv'deki en yuksek basari oranli filtre kombinasyonlarini")
    print("gercek bot koduna (analyze_symbol fonksiyonuna) ekleyecegiz.")

    # --- DETAY CSV'yi de LOG'a bas (Render Shell'e girmeden Logs sekmesinden okunabilsin) ---
    print("\n" + "=" * 70)
    print(f"DETAY CSV ICERIGI ({OUTPUT_CSV}) - asagidan kopyalayabilirsin:")
    print("=" * 70)
    print(df.to_csv(index=False))

    # --- RENDER ICIN ONEMLI: script bitince process sonlanirsa Render worker'i ---
    # --- otomatik yeniden baslatir ve backtest bastan calisir. Bunu onlemek icin ---
    # --- burada sonsuza kadar bekletiyoruz. Sonuclari Logs sekmesinden okuduktan ---
    # --- sonra Render dashboard'dan servisi SUSPEND et / eski koda geri don. ---
    log.info("Backtest tamamlandi. Sonsuz bekleme moduna geciliyor (Render restart-loop onlemi).")
    log.info("Sonuclari Logs sekmesinden kopyaladiktan sonra servisi SUSPEND edip eski botu geri koy.")
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    main()
