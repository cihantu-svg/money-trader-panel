# -*- coding: utf-8 -*-
"""
3 STRATEJI x 3 ZAMAN DILIMI BACKTEST

STRATEJILER:
  1) MA_CROSS         : EMA20 / SMA50 kesisimi (trend takibi)
                         LONG: EMA20 SMA50'yi yukari keser
                         SHORT: EMA20 SMA50'yi asagi keser
  2) RSI_MEANREV       : Trend filtreli ortalamaya donus
                         LONG: close > SMA200 VE RSI(14) 30'un altina dusuyor (asiri satim)
                         SHORT: close < SMA200 VE RSI(14) 70'in ustune cikiyor (asiri alim) [simetrik ek]
  3) BB_BREAKOUT        : Bollinger Bantlari (20, 2std) + hacim kirilimi
                         LONG: close ust bandi kirar VE hacim >= 1.3x 20-periyot ort. hacim
                         SHORT: close alt bandi kirar VE hacim >= 1.3x 20-periyot ort. hacim

ZAMAN DILIMLERI: 5m, 15m, 1h (ayni mantik, farkli TF'de test edilir)

DEGERLENDIRME: Her sinyal sonrasi FORWARD_HOURS (varsayilan 48 saat) icinde
en iyi lehte hareket olculur, %5/%10/%20 hedeflerine ulasma orani hesaplanir.
Bu, trend-takip stratejilerinin "sinyal tersine donene kadar tut" mantigini
tam yansitmaz ama tum stratejileri/TF'leri ayni olcekte kiyaslamayi saglar.

UYARI: Bu backtest agir olabilir (3 TF x N coin x 200-periyotluk indikatorler
icin yeterli gecmis veri). Once kucuk TOP_N_SYMBOLS ile dene.
"""
import time
import logging
import os
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
TIMEFRAMES = ["5m", "15m", "1h"]
TOP_N_SYMBOLS = 80
MIN_QUOTE_VOLUME_24H = 3_000_000
LOOKBACK_DAYS = 30          # 200 SMA + forward pencere icin yeterli tarihce

FORWARD_HOURS = 48           # sinyal sonrasi kac saat ileri bakilacak (tum TF'lerde ayni)
TARGET_PCTS = [5, 10, 20]

# --- MA CROSS ---
MA_FAST_PERIOD = 20         # EMA
MA_SLOW_PERIOD = 50         # SMA

# --- RSI MEAN REVERSION ---
RSI_PERIOD = 14
RSI_TREND_SMA = 200
RSI_OVERSOLD = 30
RSI_OVERBOUGHT = 70

# --- BOLLINGER BREAKOUT ---
BB_PERIOD = 20
BB_STD = 2.0
BB_VOLUME_SMA_PERIOD = 20
BB_VOLUME_MULT = 1.3

TF_MINUTES = {"5m": 5, "15m": 15, "1h": 60}

MAX_RETRIES = 3
RETRY_BACKOFF_BASE = 0.5
REQUEST_TIMEOUT = 10

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")


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
    interval_ms = TF_MINUTES[interval] * 60 * 1000
    all_rows = []
    cursor = start_ms
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
    for c in ["open", "high", "low", "close", "volume"]:
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
    return 100 - (100 / (1 + rs))


# ══════════════════════════════════════════════════════════════════
# SINYAL YAPISI + ORTAK ILERI-YON OLCUM
# ══════════════════════════════════════════════════════════════════
@dataclass
class Signal:
    strategy: str
    timeframe: str
    symbol: str
    direction: str
    signal_time: str
    max_favorable_pct: float
    hit_targets: dict = field(default_factory=dict)


def measure_forward(df, i, direction, forward_candles):
    entry_price = float(df["close"].iloc[i])
    future = df.iloc[i + 1: i + 1 + forward_candles]
    if len(future) == 0:
        return None
    if direction == "LONG":
        max_fav = (future["high"].max() - entry_price) / entry_price * 100
    else:
        max_fav = (entry_price - future["low"].min()) / entry_price * 100
    hit = {t: bool(max_fav >= t) for t in TARGET_PCTS}
    return float(max_fav), hit


def signal_time_str(df, i):
    return datetime.fromtimestamp(int(df["open_time"].iloc[i]) / 1000).strftime("%Y-%m-%d %H:%M")


# ══════════════════════════════════════════════════════════════════
# STRATEJI 1: MA CROSS (EMA20 / SMA50)
# ══════════════════════════════════════════════════════════════════
def strategy_ma_cross(symbol, tf, df, forward_candles):
    signals = []
    n = len(df)
    min_needed = MA_SLOW_PERIOD + forward_candles + 5
    if n < min_needed:
        return signals

    close = df["close"].astype(float)
    ema_fast = close.ewm(span=MA_FAST_PERIOD, adjust=False).mean()
    sma_slow = close.rolling(MA_SLOW_PERIOD).mean()

    above = ema_fast > sma_slow
    for i in range(MA_SLOW_PERIOD, n - forward_candles):
        if pd.isna(sma_slow.iloc[i]) or pd.isna(sma_slow.iloc[i - 1]):
            continue
        crossed_up = (not above.iloc[i - 1]) and above.iloc[i]
        crossed_down = above.iloc[i - 1] and (not above.iloc[i])

        direction = None
        if crossed_up:
            direction = "LONG"
        elif crossed_down:
            direction = "SHORT"
        if direction is None:
            continue

        res = measure_forward(df, i, direction, forward_candles)
        if res is None:
            continue
        max_fav, hit = res
        signals.append(Signal(
            strategy="1_MA_CROSS", timeframe=tf, symbol=symbol, direction=direction,
            signal_time=signal_time_str(df, i), max_favorable_pct=max_fav, hit_targets=hit,
        ))
    return signals


# ══════════════════════════════════════════════════════════════════
# STRATEJI 2: RSI + SMA200 MEAN REVERSION
# ══════════════════════════════════════════════════════════════════
def strategy_rsi_meanrev(symbol, tf, df, forward_candles):
    signals = []
    n = len(df)
    min_needed = RSI_TREND_SMA + forward_candles + 5
    if n < min_needed:
        return signals

    close = df["close"].astype(float)
    sma200 = close.rolling(RSI_TREND_SMA).mean()
    rsi = compute_rsi(close, RSI_PERIOD)

    for i in range(RSI_TREND_SMA, n - forward_candles):
        sma_v = sma200.iloc[i]
        r_now = rsi.iloc[i]
        r_prev = rsi.iloc[i - 1]
        if pd.isna(sma_v) or pd.isna(r_now) or pd.isna(r_prev):
            continue

        direction = None
        # LONG: ana trend yukselis (close>SMA200) VE RSI yeni 30'un altina dustu
        if close.iloc[i] > sma_v and r_prev >= RSI_OVERSOLD > r_now:
            direction = "LONG"
        # SHORT (simetrik ek): ana trend dususte (close<SMA200) VE RSI yeni 70'in ustune cikti
        elif close.iloc[i] < sma_v and r_prev <= RSI_OVERBOUGHT < r_now:
            direction = "SHORT"
        if direction is None:
            continue

        res = measure_forward(df, i, direction, forward_candles)
        if res is None:
            continue
        max_fav, hit = res
        signals.append(Signal(
            strategy="2_RSI_MEANREV", timeframe=tf, symbol=symbol, direction=direction,
            signal_time=signal_time_str(df, i), max_favorable_pct=max_fav, hit_targets=hit,
        ))
    return signals


# ══════════════════════════════════════════════════════════════════
# STRATEJI 3: BOLLINGER + HACIM BREAKOUT
# ══════════════════════════════════════════════════════════════════
def strategy_bb_breakout(symbol, tf, df, forward_candles):
    signals = []
    n = len(df)
    min_needed = max(BB_PERIOD, BB_VOLUME_SMA_PERIOD) + forward_candles + 5
    if n < min_needed:
        return signals

    close = df["close"].astype(float)
    volume = df["volume"].astype(float)
    mid = close.rolling(BB_PERIOD).mean()
    std = close.rolling(BB_PERIOD).std()
    upper = mid + BB_STD * std
    lower = mid - BB_STD * std
    vol_sma = volume.rolling(BB_VOLUME_SMA_PERIOD).mean()

    above_upper = close > upper
    below_lower = close < lower

    for i in range(max(BB_PERIOD, BB_VOLUME_SMA_PERIOD), n - forward_candles):
        if pd.isna(upper.iloc[i]) or pd.isna(lower.iloc[i]) or pd.isna(vol_sma.iloc[i]):
            continue
        vol_spike = volume.iloc[i] >= BB_VOLUME_MULT * vol_sma.iloc[i]
        if not vol_spike:
            continue

        # "yeni kirilim" - bir onceki mum zaten bandin disinda degilse
        broke_up = above_upper.iloc[i] and not above_upper.iloc[i - 1]
        broke_down = below_lower.iloc[i] and not below_lower.iloc[i - 1]

        direction = None
        if broke_up:
            direction = "LONG"
        elif broke_down:
            direction = "SHORT"
        if direction is None:
            continue

        res = measure_forward(df, i, direction, forward_candles)
        if res is None:
            continue
        max_fav, hit = res
        signals.append(Signal(
            strategy="3_BB_BREAKOUT", timeframe=tf, symbol=symbol, direction=direction,
            signal_time=signal_time_str(df, i), max_favorable_pct=max_fav, hit_targets=hit,
        ))
    return signals


# ══════════════════════════════════════════════════════════════════
# RAPOR
# ══════════════════════════════════════════════════════════════════
def summarize(all_signals):
    print("\n" + "=" * 100)
    print(f"3 STRATEJI x 3 ZAMAN DILIMI BACKTEST  ({TOP_N_SYMBOLS} coin, {LOOKBACK_DAYS} gun, forward={FORWARD_HOURS}sa)")
    print("=" * 100)

    strategies = sorted(set(s.strategy for s in all_signals))
    header = f"{'Strateji':<18} | {'TF':>4} | {'n':>6} |" + "".join(f" %{t} hit |" for t in TARGET_PCTS) + f" {'Ort.max.fav%':>13}"
    print(header)
    print("-" * len(header))

    rows = []
    for strat in strategies:
        for tf in TIMEFRAMES:
            subset = [s for s in all_signals if s.strategy == strat and s.timeframe == tf]
            n = len(subset)
            rates = {}
            for t in TARGET_PCTS:
                hits = sum(1 for s in subset if s.hit_targets.get(t))
                rates[t] = (hits / n * 100) if n else 0
            avg_fav = (sum(s.max_favorable_pct for s in subset) / n) if n else 0
            rows.append((strat, tf, n, rates, avg_fav))

    rows.sort(key=lambda r: r[3].get(10, 0), reverse=True)
    for strat, tf, n, rates, avg_fav in rows:
        row = f"{strat:<18} | {tf:>4} | {n:>6} |"
        for t in TARGET_PCTS:
            row += f" {rates[t]:>7.1f}% |"
        row += f" {avg_fav:>12.2f}%"
        print(row)

    print("\nYorum: %10 hit-rate'e gore siralandi. n<15 olan satirlarda rakamlar guvenilmezdir.")
    print("MA_CROSS ve RSI_MEANREV dogal olarak buyuk TF'lerde (1h) daha az ama daha 'temiz'")
    print("sinyal uretir; 5dk'da cok daha fazla ama gurultulu sinyal beklenir.")

    print("\n--- YON BAZINDA KIRILIM (%10 hit-rate) ---")
    for strat in strategies:
        for tf in TIMEFRAMES:
            for direction in ("LONG", "SHORT"):
                subset = [s for s in all_signals if s.strategy == strat and s.timeframe == tf and s.direction == direction]
                if not subset:
                    continue
                hits = sum(1 for s in subset if s.hit_targets.get(10))
                rate = hits / len(subset) * 100
                print(f"  {strat:<18} {tf:>4} {direction:<6} n={len(subset):>5}  %10 hit-rate={rate:.1f}%")


def save_csv(all_signals, path="three_strategies_multi_tf.csv"):
    rows = []
    for s in all_signals:
        row = {
            "strategy": s.strategy, "timeframe": s.timeframe, "symbol": s.symbol,
            "direction": s.direction, "signal_time": s.signal_time,
            "max_favorable_pct": round(s.max_favorable_pct, 2),
        }
        for t in TARGET_PCTS:
            row[f"hit_{t}pct"] = s.hit_targets.get(t)
        rows.append(row)
    pd.DataFrame(rows).to_csv(path, index=False)
    log.info(f"Detayli sonuclar kaydedildi: {path}")


def send_telegram_document(path, caption=""):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return False
    try:
        with open(path, "rb") as f:
            r = session.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendDocument",
                data={"chat_id": TELEGRAM_CHAT_ID, "caption": caption},
                files={"document": f}, timeout=60,
            )
        return r.status_code == 200
    except Exception as e:
        log.error(f"Telegram dosya gonderim hatasi: {e}")
        return False


# ══════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════
def main():
    log.info(f"Top {TOP_N_SYMBOLS} likit coin cekiliyor (min {MIN_QUOTE_VOLUME_24H/1e6:.1f}M USDT)...")
    symbols = get_top_symbols(TOP_N_SYMBOLS, MIN_QUOTE_VOLUME_24H)
    log.info(f"{len(symbols)} coin bulundu. TF'ler: {TIMEFRAMES} | Lookback: {LOOKBACK_DAYS} gun | Forward: {FORWARD_HOURS}sa")

    all_signals = []
    end_ms = int(time.time() * 1000)
    start_ms = int((datetime.now() - timedelta(days=LOOKBACK_DAYS)).timestamp() * 1000)

    for tf in TIMEFRAMES:
        forward_candles = int(FORWARD_HOURS * 60 / TF_MINUTES[tf])
        log.info(f"\n=== {tf} taraniyor (forward_candles={forward_candles}) ===")
        for idx, symbol in enumerate(symbols, 1):
            try:
                df = get_klines_range(symbol, tf, start_ms, end_ms)
                if df is None or len(df) < RSI_TREND_SMA + forward_candles + 5:
                    continue
                all_signals.extend(strategy_ma_cross(symbol, tf, df, forward_candles))
                all_signals.extend(strategy_rsi_meanrev(symbol, tf, df, forward_candles))
                all_signals.extend(strategy_bb_breakout(symbol, tf, df, forward_candles))
                if idx % 20 == 0:
                    log.info(f"  [{tf}] [{idx}/{len(symbols)}] islendi, su ana kadar {len(all_signals)} toplam sinyal")
            except Exception as e:
                log.warning(f"  [{tf}] {symbol} hata: {e}")
            time.sleep(0.05)

    if not all_signals:
        log.error("Hic sinyal bulunamadi.")
        return

    summarize(all_signals)
    save_csv(all_signals)

    if TELEGRAM_TOKEN and TELEGRAM_CHAT_ID:
        sent = send_telegram_document("three_strategies_multi_tf.csv", caption="3 strateji x 3 TF backtest sonuclari")
        if sent:
            log.info("CSV Telegram'a gonderildi.")


if __name__ == "__main__":
    main()
