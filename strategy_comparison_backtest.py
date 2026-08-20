# -*- coding: utf-8 -*-
"""
STRATEJI KARSILASTIRMA BACKTEST - 150 coin, 4 strateji, tek veri seti

Veri BIR KEZ cekilir (150 coin x N gun 5dk mum), sonra 4 farkli strateji
AYNI veri uzerinde test edilir. Boylece stratejiler adil kosullarda
kiyaslanir (ayni donem, ayni coin evreni, ayni piyasa kosullari).

STRATEJILER:
  A) SEVIYE_IMBALANCE     : taker buy/sell >= %55 VE govde >= %4 (mevcut canli bot)
  B) SIKI_IMBALANCE       : taker buy/sell >= %60 VE govde >= %6 (daha secici varyant)
  C) SMA100_DOKUNMA_TAKER : SMA100'e yeni dokunma + o mumda taker baskinligi >= %60
  D) HACIM_RSI            : hacim >= 1.5x 20-periyot hacim ortalamasi VE RSI(14) 50 kesisimi

Her strateji icin sinyal sonrasi FORWARD_CANDLES icinde en iyi lehte hareket
olculur, %5/%10/%20 hedeflerine ulasma orani (hit-rate) hesaplanir.

SONUC: konsolda siralanmis karsilastirma tablosu + strategy_comparison.csv
(tum sinyallerin detayi, hangi stratejiden geldigi ile birlikte)
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
# GENEL AYARLAR
# ══════════════════════════════════════════════════════════════════
TIMEFRAME = "5m"
LOOKBACK_DAYS = 15
TOP_N_SYMBOLS = 150
MIN_QUOTE_VOLUME_24H = 5_000_000

FORWARD_CANDLES = 36            # sinyal sonrasi kac mum ileri bakilacak (36x5dk=3 saat)
TARGET_PCTS = [5, 10, 20]

# --- STRATEJI A: SEVIYE_IMBALANCE (mevcut canli bot) ---
A_IMBALANCE_RATIO = 0.55
A_MIN_BODY_PCT = 4.0

# --- STRATEJI B: SIKI_IMBALANCE ---
B_IMBALANCE_RATIO = 0.60
B_MIN_BODY_PCT = 6.0

# --- STRATEJI C: SMA100_DOKUNMA_TAKER ---
C_SMA_PERIOD = 100
C_TOUCH_TOLERANCE_PCT = 0.15
C_IMBALANCE_RATIO = 0.60

# --- STRATEJI D: HACIM_RSI ---
D_VOLUME_SMA_PERIOD = 20
D_VOLUME_SPIKE_MULT = 1.5
D_RSI_PERIOD = 14
D_RSI_LEVEL = 50.0

MAX_RETRIES = 3
RETRY_BACKOFF_BASE = 0.5
REQUEST_TIMEOUT = 10

MIN_HISTORY_NEEDED = max(C_SMA_PERIOD, D_VOLUME_SMA_PERIOD, D_RSI_PERIOD) + FORWARD_CANDLES + 10


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
# VERI CEKME (tek seferlik, tum stratejiler bunu paylasir)
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
        time.sleep(0.08)

    if not all_rows:
        return None
    df = pd.DataFrame(all_rows, columns=[
        "open_time", "open", "high", "low", "close", "volume", "close_time",
        "qav", "trades", "taker_buy_base", "taker_buy_quote", "ignore"])
    for c in ["open", "high", "low", "close", "volume", "taker_buy_base"]:
        df[c] = df[c].astype(float)
    df = df.drop_duplicates(subset="open_time").reset_index(drop=True)
    return df


def compute_rsi(close, period=14):
    delta = close.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = -delta.where(delta < 0, 0.0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    return rsi


# ══════════════════════════════════════════════════════════════════
# SINYAL YAPISI + ORTAK ILERI-YON OLCUM FONKSIYONU
# ══════════════════════════════════════════════════════════════════
@dataclass
class StratSignal:
    strategy: str
    symbol: str
    direction: str
    signal_time: str
    entry_price: float
    max_favorable_pct: float
    hit_targets: dict = field(default_factory=dict)


def measure_forward(df, i, direction):
    entry_price = float(df["close"].iloc[i])
    future = df.iloc[i + 1: i + 1 + FORWARD_CANDLES]
    if direction == "LONG":
        max_fav = (future["high"].max() - entry_price) / entry_price * 100
    else:
        max_fav = (entry_price - future["low"].min()) / entry_price * 100
    hit = {t: bool(max_fav >= t) for t in TARGET_PCTS}
    return entry_price, float(max_fav), hit


# ══════════════════════════════════════════════════════════════════
# STRATEJI A + B: SEVIYE BAZLI IMBALANCE (parametrik, ayni fonksiyon)
# ══════════════════════════════════════════════════════════════════
def strategy_level_imbalance(symbol, df, name, imbalance_ratio, min_body_pct):
    signals = []
    n = len(df)
    if n < MIN_HISTORY_NEEDED:
        return signals

    close = df["close"].astype(float)
    open_ = df["open"].astype(float)
    vol_safe = df["volume"].replace(0, np.nan).astype(float)
    buy_ratio = (df["taker_buy_base"].astype(float) / vol_safe)
    body_pct = (close - open_) / open_ * 100

    for i in range(1, n - FORWARD_CANDLES):
        br = buy_ratio.iloc[i]
        bp = body_pct.iloc[i]
        if pd.isna(br) or pd.isna(bp):
            continue
        sr = 1.0 - br

        direction = None
        if br >= imbalance_ratio and bp >= min_body_pct:
            direction = "LONG"
        elif sr >= imbalance_ratio and bp <= -min_body_pct:
            direction = "SHORT"
        if direction is None:
            continue

        entry_price, max_fav, hit = measure_forward(df, i, direction)
        signals.append(StratSignal(
            strategy=name, symbol=symbol, direction=direction,
            signal_time=datetime.fromtimestamp(int(df["open_time"].iloc[i]) / 1000).strftime("%Y-%m-%d %H:%M"),
            entry_price=entry_price, max_favorable_pct=max_fav, hit_targets=hit,
        ))
    return signals


# ══════════════════════════════════════════════════════════════════
# STRATEJI C: SMA100 DOKUNMA + TAKER BASKINLIGI
# ══════════════════════════════════════════════════════════════════
def strategy_sma100_touch(symbol, df):
    signals = []
    n = len(df)
    if n < MIN_HISTORY_NEEDED:
        return signals

    close = df["close"].astype(float)
    open_ = df["open"].astype(float)
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    vol_safe = df["volume"].replace(0, np.nan).astype(float)
    buy_ratio = (df["taker_buy_base"].astype(float) / vol_safe)

    sma = close.rolling(C_SMA_PERIOD).mean()
    tol = sma * (C_TOUCH_TOLERANCE_PCT / 100.0)
    touching = (low <= sma + tol) & (high >= sma - tol) & sma.notna()

    for i in range(C_SMA_PERIOD, n - FORWARD_CANDLES):
        if not touching.iloc[i] or touching.iloc[i - 1]:
            continue

        br = buy_ratio.iloc[i]
        if pd.isna(br):
            continue
        sr = 1.0 - br

        if close.iloc[i] > open_.iloc[i]:
            direction, taker_ratio = "LONG", br
        elif close.iloc[i] < open_.iloc[i]:
            direction, taker_ratio = "SHORT", sr
        else:
            continue

        if taker_ratio < C_IMBALANCE_RATIO:
            continue

        entry_price, max_fav, hit = measure_forward(df, i, direction)
        signals.append(StratSignal(
            strategy="C_SMA100_DOKUNMA_TAKER", symbol=symbol, direction=direction,
            signal_time=datetime.fromtimestamp(int(df["open_time"].iloc[i]) / 1000).strftime("%Y-%m-%d %H:%M"),
            entry_price=entry_price, max_favorable_pct=max_fav, hit_targets=hit,
        ))
    return signals


# ══════════════════════════════════════════════════════════════════
# STRATEJI D: HACIM SPIKE + RSI50 KESISIMI
# ══════════════════════════════════════════════════════════════════
def strategy_volume_rsi(symbol, df):
    signals = []
    n = len(df)
    if n < MIN_HISTORY_NEEDED:
        return signals

    close = df["close"].astype(float)
    volume = df["volume"].astype(float)
    vol_sma = volume.rolling(D_VOLUME_SMA_PERIOD).mean()
    rsi = compute_rsi(close, D_RSI_PERIOD)

    for i in range(D_VOLUME_SMA_PERIOD + D_RSI_PERIOD, n - FORWARD_CANDLES):
        vs = vol_sma.iloc[i]
        if pd.isna(vs) or vs <= 0:
            continue
        vol_spike = volume.iloc[i] >= D_VOLUME_SPIKE_MULT * vs

        r_now = rsi.iloc[i]
        r_prev = rsi.iloc[i - 1]
        if pd.isna(r_now) or pd.isna(r_prev):
            continue

        direction = None
        if vol_spike and r_prev < D_RSI_LEVEL <= r_now:
            direction = "LONG"
        elif vol_spike and r_prev >= D_RSI_LEVEL > r_now:
            direction = "SHORT"
        if direction is None:
            continue

        entry_price, max_fav, hit = measure_forward(df, i, direction)
        signals.append(StratSignal(
            strategy="D_HACIM_RSI", symbol=symbol, direction=direction,
            signal_time=datetime.fromtimestamp(int(df["open_time"].iloc[i]) / 1000).strftime("%Y-%m-%d %H:%M"),
            entry_price=entry_price, max_favorable_pct=max_fav, hit_targets=hit,
        ))
    return signals


# ══════════════════════════════════════════════════════════════════
# ANALIZ / RAPOR
# ══════════════════════════════════════════════════════════════════
def summarize(all_signals):
    strategies = sorted(set(s.strategy for s in all_signals))
    print("\n" + "=" * 90)
    print(f"STRATEJI KARSILASTIRMA - {TOP_N_SYMBOLS} coin, son {LOOKBACK_DAYS} gun, {TIMEFRAME}")
    print("=" * 90)

    header = f"{'Strateji':<28} | {'Sinyal':>7} |" + "".join(f" %{t} hit |" for t in TARGET_PCTS) + f" {'Ort.max.fav%':>13}"
    print(header)
    print("-" * len(header))

    rows = []
    for strat in strategies:
        subset = [s for s in all_signals if s.strategy == strat]
        n = len(subset)
        rates = {}
        for t in TARGET_PCTS:
            hits = sum(1 for s in subset if s.hit_targets.get(t))
            rates[t] = (hits / n * 100) if n else 0
        avg_fav = (sum(s.max_favorable_pct for s in subset) / n) if n else 0
        rows.append((strat, n, rates, avg_fav))

    # %10 hit-rate'e gore sirala (ana kiyaslama metrigi)
    rows.sort(key=lambda r: r[2].get(10, 0), reverse=True)

    for strat, n, rates, avg_fav in rows:
        row = f"{strat:<28} | {n:>7} |"
        for t in TARGET_PCTS:
            row += f" {rates[t]:>7.1f}% |"
        row += f" {avg_fav:>12.2f}%"
        print(row)

    print("\nSiralama %10 hedefe ulasma oranina gore (buyukten kucuge). Ort.max.fav%,")
    print("sinyal basina ortalama en iyi lehte hareketi gosterir (basari BUYUKLUGU icin,")
    print("hit-rate ise basari SIKLIGI icin - ikisine birlikte bakmak daha saglikli.")
    print("Az sayida sinyal ureten stratejilerde (n<30) rakamlar guvenilmezdir, dikkatli yorumla.")

    # yon bazinda kirilim
    print("\n--- YON BAZINDA KIRILIM (%10 hit-rate) ---")
    for strat in strategies:
        for direction in ("LONG", "SHORT"):
            subset = [s for s in all_signals if s.strategy == strat and s.direction == direction]
            if not subset:
                continue
            hits = sum(1 for s in subset if s.hit_targets.get(10))
            rate = hits / len(subset) * 100
            print(f"  {strat:<28} {direction:<6} n={len(subset):>5}  %10 hit-rate={rate:.1f}%")


def save_csv(all_signals, path="strategy_comparison.csv"):
    rows = []
    for s in all_signals:
        row = {
            "strategy": s.strategy, "symbol": s.symbol, "direction": s.direction,
            "signal_time": s.signal_time, "entry_price": s.entry_price,
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
    log.info(f"{len(symbols)} coin bulundu. Backtest: son {LOOKBACK_DAYS} gun, TF={TIMEFRAME}, 4 strateji birlikte test edilecek")

    end_ms = int(time.time() * 1000)
    start_ms = int((datetime.now() - timedelta(days=LOOKBACK_DAYS)).timestamp() * 1000)

    all_signals = []
    for idx, symbol in enumerate(symbols, 1):
        try:
            df = get_klines_range(symbol, TIMEFRAME, start_ms, end_ms)
            if df is None or len(df) < MIN_HISTORY_NEEDED:
                continue

            all_signals.extend(strategy_level_imbalance(symbol, df, "A_SEVIYE_IMBALANCE", A_IMBALANCE_RATIO, A_MIN_BODY_PCT))
            all_signals.extend(strategy_level_imbalance(symbol, df, "B_SIKI_IMBALANCE", B_IMBALANCE_RATIO, B_MIN_BODY_PCT))
            all_signals.extend(strategy_sma100_touch(symbol, df))
            all_signals.extend(strategy_volume_rsi(symbol, df))

            if idx % 15 == 0:
                log.info(f"[{idx}/{len(symbols)}] islendi, su ana kadar {len(all_signals)} toplam sinyal (4 strateji birlikte)")
        except Exception as e:
            log.warning(f"{symbol} hata: {e}")
        time.sleep(0.05)

    if not all_signals:
        log.error("Hic sinyal bulunamadi.")
        return

    summarize(all_signals)
    save_csv(all_signals)


if __name__ == "__main__":
    main()
