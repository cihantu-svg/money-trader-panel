# -*- coding: utf-8 -*-
"""
SMA100 DOKUNMA + TAKER BASKINLIGI BACKTEST

SORU: 5dk grafikte fiyat SMA100'e dokundugunda, o mumda taker buy/sell
baskinligi (>= belirli bir esik, orn. %60) varsa, bu dokunmanin lehte
sonuclanma orani (hit-rate) artiyor mu?

MANTIK:
1) Her kapanmis 5dk mumda SMA100 hesaplanir.
2) "Dokunma" = mumun (low, high) araligi SMA100 degerini iceriyor
   (kucuk bir tolerans payi ile). Sadece dokunma ANINA (onceki mum
   dokunmuyorken bu mum dokunuyorsa) bakilir - ayni bolgede art arda
   dokunan mumlar tek sinyal sayilir (whipsaw/chop sismesini onler).
3) Yon, mumun kendi renginden belirlenir: yesil mum (close>open) -> LONG
   beklentisi (SMA100'den yukari sekme), kirmizi mum (close<open) -> SHORT
   beklentisi (SMA100'den asagi red).
4) BASELINE grubu: taker filtresi olmadan TUM dokunmalar.
   FILTRELI gruplar: o mumun taker orani (LONG icin buy_ratio, SHORT icin
   sell_ratio) belirli esikleri (%50/%55/%60/%65/%70) gecen alt kumeler.
5) Her grup icin sinyal sonrasi FORWARD_CANDLES icinde en iyi lehte
   harekesin %5/%10/%20 hedeflerine ulasma orani olculur.

CIKTI: konsola karsilastirma tablosu + sma100_touch_results.csv
"""
import time
import logging
from datetime import datetime, timedelta
from dataclasses import dataclass, field

import numpy as np
import requests
import pandas as pd

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger(__name__)

BINANCE_BASE = "https://fapi.binance.com"
session = requests.Session()

# ══════════════════════════════════════════════════════════════════
# AYARLAR
# ══════════════════════════════════════════════════════════════════
TIMEFRAME = "5m"
LOOKBACK_DAYS = 15
TOP_N_SYMBOLS = 80
MIN_QUOTE_VOLUME_24H = 5_000_000

SMA_PERIOD = 100
TOUCH_TOLERANCE_PCT = 0.15   # SMA100'e mumun high/low'u bu yuzde kadar yakinsa "dokunma" sayilir

# Test edilecek taker imbalance esikleri (0 = filtre yok / baseline)
IMBALANCE_THRESHOLDS = [0.0, 0.50, 0.55, 0.60, 0.65, 0.70]

FORWARD_CANDLES = 36          # sinyal sonrasi kac mum ileri bakilacak (36x5dk=3 saat)
TARGET_PCTS = [5, 10, 20]

MAX_RETRIES = 3
RETRY_BACKOFF_BASE = 0.5
REQUEST_TIMEOUT = 10


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
                log.warning(f"HTTP {r.status_code}, {wait:.1f}sn sonra tekrar (deneme {attempt+1})")
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
# VERI CEKME
# ══════════════════════════════════════════════════════════════════
def get_top_symbols(top_n, min_qv):
    r = _request_with_retry(f"{BINANCE_BASE}/fapi/v1/ticker/24hr", timeout=15)
    data = r.json()
    info = _request_with_retry(f"{BINANCE_BASE}/fapi/v1/exchangeInfo", timeout=15).json()
    trading = {s["symbol"] for s in info["symbols"] if s["status"] == "TRADING" and s["symbol"].endswith("USDT")}
    rows = [(d["symbol"], float(d.get("quoteVolume", 0))) for d in data
            if d["symbol"] in trading and float(d.get("quoteVolume", 0)) >= min_qv]
    rows.sort(key=lambda x: x[1], reverse=True)
    return [s for s, _ in rows[:top_n]]


def get_klines_range(symbol, interval, start_ms, end_ms):
    all_rows = []
    cursor = start_ms
    interval_ms = 5 * 60 * 1000
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
        last_open = raw[-1][0]
        next_cursor = last_open + interval_ms
        if next_cursor <= cursor:
            break
        cursor = next_cursor
        if len(raw) < 1500:
            break
        time.sleep(0.1)

    if not all_rows:
        return None
    df = pd.DataFrame(all_rows, columns=[
        "open_time", "open", "high", "low", "close", "volume", "close_time",
        "qav", "trades", "taker_buy_base", "taker_buy_quote", "ignore"])
    for c in ["open", "high", "low", "close", "volume", "taker_buy_base"]:
        df[c] = df[c].astype(float)
    df = df.drop_duplicates(subset="open_time").reset_index(drop=True)
    return df


# ══════════════════════════════════════════════════════════════════
# DOKUNMA TESPITI + SINYAL
# ══════════════════════════════════════════════════════════════════
@dataclass
class TouchSignal:
    symbol: str
    direction: str
    touch_time: str
    entry_price: float
    taker_ratio: float       # yon ile uyumlu taker orani (LONG->buy, SHORT->sell)
    max_favorable_pct: float
    hit_targets: dict = field(default_factory=dict)


def compute_touch_signals(symbol, df):
    signals = []
    n = len(df)
    if n < SMA_PERIOD + FORWARD_CANDLES + 5:
        return signals

    close = df["close"].astype(float)
    open_ = df["open"].astype(float)
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    vol_safe = df["volume"].replace(0, np.nan).astype(float)
    buy_ratio = (df["taker_buy_base"].astype(float) / vol_safe).astype(float)

    sma100 = close.rolling(SMA_PERIOD).mean()

    tol = sma100 * (TOUCH_TOLERANCE_PCT / 100.0)
    touching = (low <= sma100 + tol) & (high >= sma100 - tol) & sma100.notna()

    for i in range(SMA_PERIOD, n - FORWARD_CANDLES):
        if not touching.iloc[i]:
            continue
        # sadece "yeni" dokunma - bir onceki mum zaten dokunuyorsa atla (chop sismesini onle)
        if i > 0 and touching.iloc[i - 1]:
            continue

        br = buy_ratio.iloc[i]
        if pd.isna(br):
            continue
        sr = 1.0 - br

        is_green = close.iloc[i] > open_.iloc[i]
        if is_green:
            direction = "LONG"
            taker_ratio = br
        elif close.iloc[i] < open_.iloc[i]:
            direction = "SHORT"
            taker_ratio = sr
        else:
            continue  # doji, yon belirsiz

        entry_price = float(close.iloc[i])
        future = df.iloc[i + 1: i + 1 + FORWARD_CANDLES]
        if direction == "LONG":
            max_fav = (future["high"].max() - entry_price) / entry_price * 100
        else:
            max_fav = (entry_price - future["low"].min()) / entry_price * 100

        hit = {t: bool(max_fav >= t) for t in TARGET_PCTS}

        signals.append(TouchSignal(
            symbol=symbol,
            direction=direction,
            touch_time=datetime.fromtimestamp(int(df["open_time"].iloc[i]) / 1000).strftime("%Y-%m-%d %H:%M"),
            entry_price=entry_price,
            taker_ratio=float(taker_ratio),
            max_favorable_pct=float(max_fav),
            hit_targets=hit,
        ))
    return signals


# ══════════════════════════════════════════════════════════════════
# ANALIZ / RAPOR
# ══════════════════════════════════════════════════════════════════
def summarize(signals):
    print("\n" + "=" * 78)
    print(f"TOPLAM SMA100 DOKUNMASI (taker filtresi yok - baseline): {len(signals)}")
    print("=" * 78)

    header = f"{'Taker esigi':>12} | {'Sinyal sayisi':>14} |" + "".join(f" %{t} hit-rate |" for t in TARGET_PCTS)
    print(header)
    print("-" * len(header))
    for th in IMBALANCE_THRESHOLDS:
        subset = [s for s in signals if s.taker_ratio >= th]
        row = f"{th*100:>10.0f}% | {len(subset):>14} |"
        for t in TARGET_PCTS:
            hits = sum(1 for s in subset if s.hit_targets.get(t))
            rate = (hits / len(subset) * 100) if subset else 0
            row += f" {rate:>9.1f}% |"
        print(row)

    print("\nYorum: '0%' satiri hicbir taker filtresi olmadan SADECE SMA100 dokunmasinin")
    print("hit-rate'i (baseline). Esik yukseldikce hit-rate baseline'in USTUNE cikiyorsa,")
    print("'SMA100 dokunmasi + taker baskinligi' kombinasyonu gercekten degerli demektir.")
    print("Hit-rate baseline civarinda kaliyor/dususe geciyorsa, taker filtresi bu ozel")
    print("senaryoda (dokunma anlarinda) ayirt edici degildir.")

    # LONG / SHORT ayri kirilim (yon bazinda farkli davranabilir)
    for direction in ("LONG", "SHORT"):
        dsigs = [s for s in signals if s.direction == direction]
        if not dsigs:
            continue
        print(f"\n--- {direction} dokunmalari (n={len(dsigs)}) ---")
        for th in IMBALANCE_THRESHOLDS:
            subset = [s for s in dsigs if s.taker_ratio >= th]
            if not subset:
                continue
            hits10 = sum(1 for s in subset if s.hit_targets.get(10))
            rate10 = hits10 / len(subset) * 100
            print(f"  taker>=%{th*100:.0f}: n={len(subset):>4}  %10 hit-rate={rate10:.1f}%")


def save_csv(signals, path="sma100_touch_results.csv"):
    rows = []
    for s in signals:
        row = {
            "symbol": s.symbol, "direction": s.direction, "touch_time": s.touch_time,
            "entry_price": s.entry_price, "taker_ratio": round(s.taker_ratio, 4),
            "max_favorable_pct": round(s.max_favorable_pct, 2),
        }
        for t in TARGET_PCTS:
            row[f"hit_{t}pct"] = s.hit_targets.get(t)
        rows.append(row)
    pd.DataFrame(rows).to_csv(path, index=False)
    log.info(f"Detayli sonuclar kaydedildi: {path}")


# ══════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════
def main():
    log.info(f"Top {TOP_N_SYMBOLS} likit coin cekiliyor (min {MIN_QUOTE_VOLUME_24H/1e6:.1f}M USDT)...")
    symbols = get_top_symbols(TOP_N_SYMBOLS, MIN_QUOTE_VOLUME_24H)
    log.info(f"{len(symbols)} coin bulundu. Backtest: son {LOOKBACK_DAYS} gun, TF={TIMEFRAME}, SMA{SMA_PERIOD}")

    end_ms = int(time.time() * 1000)
    start_ms = int((datetime.now() - timedelta(days=LOOKBACK_DAYS)).timestamp() * 1000)

    all_signals = []
    for idx, symbol in enumerate(symbols, 1):
        try:
            df = get_klines_range(symbol, TIMEFRAME, start_ms, end_ms)
            if df is None or len(df) < SMA_PERIOD + FORWARD_CANDLES + 5:
                continue
            sigs = compute_touch_signals(symbol, df)
            all_signals.extend(sigs)
            if idx % 10 == 0:
                log.info(f"[{idx}/{len(symbols)}] islendi, su ana kadar {len(all_signals)} dokunma")
        except Exception as e:
            log.warning(f"{symbol} hata: {e}")
        time.sleep(0.05)

    if not all_signals:
        log.error("Hic dokunma bulunamadi - TOUCH_TOLERANCE_PCT'yi artirmayi dene.")
        return

    summarize(all_signals)
    save_csv(all_signals)


if __name__ == "__main__":
    main()
