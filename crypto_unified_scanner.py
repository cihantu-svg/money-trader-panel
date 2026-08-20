# -*- coding: utf-8 -*-
"""
UNIFIED BACKTEST + LEAD-LAG SCANNER + TELEGRAM EXPORT
======================================================
Tek scriptte:
  1) 4 Strateji Backtest (A/B/C/D) - ayni veri seti uzerinde
  2) Lead-Lag / Copy-Cat Scanner  - %7 esik, 2 saat pencere
  3) Data Analizi + Ozet Rapor
  4) CSV ciktilari
  5) Telegram'a otomatik gonderim

Ortam degiskenleri:
  export TELEGRAM_TOKEN="123456:ABC..."
  export TELEGRAM_CHAT_ID="-1001234567890"

Calistir:
  python unified_scanner.py
"""
import os
import time
import logging
import subprocess
from datetime import datetime, timedelta
from dataclasses import dataclass, field
from collections import defaultdict

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
MIN_QUOTE_VOLUME_24H = 3_000_000

FORWARD_CANDLES = 36            # backtest: sinyal sonrasi 3 saat
TARGET_PCTS = [5, 10, 20]

# --- STRATEJI PARAMETRELERI ---
A_IMBALANCE_RATIO = 0.55;  A_MIN_BODY_PCT = 4.0
B_IMBALANCE_RATIO = 0.60;  B_MIN_BODY_PCT = 6.0
C_SMA_PERIOD = 100;        C_TOUCH_TOLERANCE_PCT = 0.15;  C_IMBALANCE_RATIO = 0.60
D_VOLUME_SMA_PERIOD = 20;  D_VOLUME_SPIKE_MULT = 1.5;     D_RSI_PERIOD = 14;  D_RSI_LEVEL = 50.0

# --- LEAD-LAG AYARLARI ---
LL_EVENT_PCT = 7.0
LL_FORWARD_CANDLES = 24         # 2 saat = 24 x 5dk
LL_EVENT_MODE = "close_vs_open" # "close_vs_open" veya "close_vs_prev_close"

MIN_HISTORY_NEEDED = max(C_SMA_PERIOD, D_VOLUME_SMA_PERIOD, D_RSI_PERIOD) + FORWARD_CANDLES + 10

MAX_RETRIES = 3
RETRY_BACKOFF_BASE = 0.5
REQUEST_TIMEOUT = 10


# ══════════════════════════════════════════════════════════════════
# TELEGRAM GONDERIM
# ══════════════════════════════════════════════════════════════════
def send_telegram_document(filepath, caption=""):
    token = os.environ.get("TELEGRAM_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        log.warning("TELEGRAM_TOKEN veya TELEGRAM_CHAT_ID bulunamadi. Telegram gonderimi atlaniyor.")
        log.info("Ornek export: export TELEGRAM_TOKEN='123456:ABC...'")
        log.info("Ornek export: export TELEGRAM_CHAT_ID='-1001234567890'")
        return False

    url = f"https://api.telegram.org/bot{token}/sendDocument"
    try:
        with open(filepath, "rb") as f:
            files = {"document": f}
            data = {"chat_id": chat_id, "caption": caption}
            r = requests.post(url, data=data, files=files, timeout=30)
        if r.status_code == 200:
            log.info(f"Telegram'a gonderildi: {filepath}")
            return True
        else:
            log.warning(f"Telegram gonderim hatasi: HTTP {r.status_code} - {r.text[:200]}")
            return False
    except Exception as e:
        log.warning(f"Telegram gonderim hatasi: {e}")
        return False


# ══════════════════════════════════════════════════════════════════
# VERI CEKME
# ══════════════════════════════════════════════════════════════════
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
# STRATEJI BACKTEST YAPILARI
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


def summarize_strategies(all_signals):
    strategies = sorted(set(s.strategy for s in all_signals))
    print("\n" + "=" * 100)
    print(f"STRATEJI KARSILASTIRMA - {TOP_N_SYMBOLS} coin, son {LOOKBACK_DAYS} gun, {TIMEFRAME}")
    print("=" * 100)
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
    rows.sort(key=lambda r: r[2].get(10, 0), reverse=True)
    for strat, n, rates, avg_fav in rows:
        row = f"{strat:<28} | {n:>7} |"
        for t in TARGET_PCTS:
            row += f" {rates[t]:>7.1f}% |"
        row += f" {avg_fav:>12.2f}%"
        print(row)
    print("\nSiralama %10 hedefe ulasma oranina gore (buyukten kucuge).")
    print("Az sayida sinyal ureten stratejilerde (n<30) rakamlar guvenilmezdir.")
    return rows


def save_strategy_csv(all_signals, path="strategy_comparison.csv"):
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
    log.info(f"Strateji detaylari kaydedildi: {path}")
    return path


# ══════════════════════════════════════════════════════════════════
# LEAD-LAG / COPY-CAT SCANNER
# ══════════════════════════════════════════════════════════════════
def find_ll_events(df, symbol, event_pct, mode):
    events = []
    n = len(df)
    if n < 2:
        return events
    for i in range(n):
        t = int(df["open_time"].iloc[i])
        t_str = datetime.fromtimestamp(t / 1000).strftime("%Y-%m-%d %H:%M")
        open_p = float(df["open"].iloc[i])
        close_p = float(df["close"].iloc[i])
        if mode == "close_vs_open":
            long_cond = close_p >= open_p * (1 + event_pct / 100)
            short_cond = close_p <= open_p * (1 - event_pct / 100)
            long_pct = (close_p / open_p - 1) * 100
            short_pct = (1 - close_p / open_p) * 100
        else:
            if i == 0:
                continue
            prev_close = float(df["close"].iloc[i - 1])
            if prev_close == 0:
                continue
            long_cond = close_p >= prev_close * (1 + event_pct / 100)
            short_cond = close_p <= prev_close * (1 - event_pct / 100)
            long_pct = (close_p / prev_close - 1) * 100
            short_pct = (1 - close_p / prev_close) * 100
        if long_cond:
            events.append({
                "symbol": symbol, "direction": "LONG", "time": t, "time_str": t_str,
                "open": open_p, "close": close_p, "pct": round(long_pct, 2),
            })
        elif short_cond:
            events.append({
                "symbol": symbol, "direction": "SHORT", "time": t, "time_str": t_str,
                "open": open_p, "close": close_p, "pct": round(short_pct, 2),
            })
    return events


def analyze_lead_lag(all_events, symbols, forward_candles):
    interval_ms = 5 * 60 * 1000
    by_sym_dir = defaultdict(list)
    for e in all_events:
        by_sym_dir[(e["symbol"], e["direction"])].append(e)
    for key in by_sym_dir:
        by_sym_dir[key].sort(key=lambda x: x["time"])

    details = []
    summary = defaultdict(lambda: {"leader_count": 0, "followed_by": defaultdict(int),
                                    "total_follows": 0, "lag_mins_list": []})

    for leader_sym in symbols:
        for direction in ("LONG", "SHORT"):
            leader_events = by_sym_dir.get((leader_sym, direction), [])
            for le in leader_events:
                skey = (leader_sym, direction)
                summary[skey]["leader_count"] += 1
                window_start = le["time"] + interval_ms
                window_end = le["time"] + forward_candles * interval_ms
                for follower_sym in symbols:
                    if follower_sym == leader_sym:
                        continue
                    follower_events = by_sym_dir.get((follower_sym, direction), [])
                    for fe in follower_events:
                        if window_start <= fe["time"] <= window_end:
                            lag_ms = fe["time"] - le["time"]
                            lag_mins = lag_ms / (60 * 1000)
                            details.append({
                                "leader": leader_sym, "leader_time": le["time_str"],
                                "leader_pct": le["pct"], "follower": follower_sym,
                                "follower_time": fe["time_str"], "follower_pct": fe["pct"],
                                "direction": direction, "lag_mins": int(lag_mins),
                            })
                            summary[skey]["followed_by"][follower_sym] += 1
                            summary[skey]["total_follows"] += 1
                            summary[skey]["lag_mins_list"].append(lag_mins)
    return details, summary


def save_ll_csv(details, summary, path="lead_lag_results.csv"):
    # Detay CSV
    if details:
        pd.DataFrame(details).to_csv(path, index=False)
        log.info(f"Lead-Lag detaylari kaydedildi: {path}  ({len(details)} takip olayi)")
    else:
        log.warning("Lead-Lag: Hic takip olayi bulunamadi.")

    # Ozet CSV (ayri dosya)
    rows = []
    for (leader, direction), stats in summary.items():
        lc = stats["leader_count"]
        if lc == 0:
            continue
        followers = sorted(stats["followed_by"].items(), key=lambda x: x[1], reverse=True)
        for follower, fcount in followers:
            avg_lag = np.mean(stats["lag_mins_list"]) if stats["lag_mins_list"] else 0
            rows.append({
                "leader": leader, "direction": direction, "leader_events": lc,
                "follower": follower, "follow_count": fcount,
                "follow_rate_pct": round(fcount / lc * 100, 1),
                "avg_lag_mins": round(avg_lag, 1),
            })
    if rows:
        sum_path = path.replace(".csv", "_summary.csv")
        pd.DataFrame(rows).to_csv(sum_path, index=False)
        log.info(f"Lead-Lag ozet kaydedildi: {sum_path}")
        return path, sum_path
    return path, None


def print_ll_summary(summary):
    print("\n" + "=" * 100)
    print("COIN COPY / LEAD-LAG OZET (%7 esik, 2 saat pencere)")
    print("=" * 100)
    print(f"{'Lider':<12} | {'Yon':<6} | {'Evnt':>5} | {'Takipci':<12} | {'Tkp':>4} | {'Oran':>6} | {'Ort.Gecikme':>11}")
    print("-" * 100)
    for (leader, direction), stats in sorted(summary.items(), key=lambda x: x[1]["total_follows"], reverse=True):
        lc = stats["leader_count"]
        if lc == 0:
            continue
        followers = sorted(stats["followed_by"].items(), key=lambda x: x[1], reverse=True)[:5]  # top 5
        for follower, fcount in followers:
            avg_lag = np.mean(stats["lag_mins_list"]) if stats["lag_mins_list"] else 0
            print(f"{leader:<12} | {direction:<6} | {lc:>5} | {follower:<12} | {fcount:>4} | {fcount/lc*100:>5.1f}% | {avg_lag:>10.1f} dk")
    print("\nNot: 'Oran' = Lider'in %7 event'larinin yuzde kaci sonrasinda")
    print("     bu takipci de ayni yonde %7 yapmistir (2 saat icinde).")


# ══════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════
def main():
    log.info(f"Top {TOP_N_SYMBOLS} likit coin cekiliyor (min {MIN_QUOTE_VOLUME_24H/1e6:.0f}M USDT)...")
    symbols = get_top_symbols(TOP_N_SYMBOLS, MIN_QUOTE_VOLUME_24H)
    log.info(f"{len(symbols)} coin bulundu. Veri cekiliyor...")

    end_ms = int(time.time() * 1000)
    start_ms = int((datetime.now() - timedelta(days=LOOKBACK_DAYS)).timestamp() * 1000)

    all_signals = []
    all_ll_events = []
    symbol_data = {}  # Lead-Lag icin tekrar cekmeyelim

    for idx, symbol in enumerate(symbols, 1):
        try:
            df = get_klines_range(symbol, TIMEFRAME, start_ms, end_ms)
            if df is None or len(df) < 10:
                continue
            symbol_data[symbol] = df

            # --- STRATEJI BACKTEST ---
            if len(df) >= MIN_HISTORY_NEEDED:
                all_signals.extend(strategy_level_imbalance(symbol, df, "A_SEVIYE_IMBALANCE", A_IMBALANCE_RATIO, A_MIN_BODY_PCT))
                all_signals.extend(strategy_level_imbalance(symbol, df, "B_SIKI_IMBALANCE", B_IMBALANCE_RATIO, B_MIN_BODY_PCT))
                all_signals.extend(strategy_sma100_touch(symbol, df))
                all_signals.extend(strategy_volume_rsi(symbol, df))

            # --- LEAD-LAG EVENTLER ---
            all_ll_events.extend(find_ll_events(df, symbol, LL_EVENT_PCT, LL_EVENT_MODE))

            if idx % 15 == 0:
                log.info(f"[{idx}/{len(symbols)}] islendi | Strateji sinyalleri: {len(all_signals)} | LL event: {len(all_ll_events)}")
        except Exception as e:
            log.warning(f"{symbol} hata: {e}")
        time.sleep(0.05)

    # ═══════════════════════════════════════════════════════════════
    # RAPORLAMA
    # ═══════════════════════════════════════════════════════════════
    print("\n" + "=" * 100)
    print("UNIFIED BACKTEST + LEAD-LAG SCANNER - SONUCLAR")
    print("=" * 100)

    # --- Strateji Raporu ---
    strat_rows = []
    if all_signals:
        strat_rows = summarize_strategies(all_signals)
        strat_path = save_strategy_csv(all_signals)
    else:
        log.warning("Hic strateji sinyali bulunamadi.")

    # --- Lead-Lag Raporu ---
    ll_details, ll_summary = analyze_lead_lag(all_ll_events, symbols, LL_FORWARD_CANDLES)
    print_ll_summary(ll_summary)
    ll_path, ll_sum_path = save_ll_csv(ll_details, ll_summary)

    # ═══════════════════════════════════════════════════════════════
    # TELEGRAM GONDERIM
    # ═══════════════════════════════════════════════════════════════
    print("\n" + "=" * 100)
    print("TELEGRAM GONDERIMI")
    print("=" * 100)

    files_to_send = []
    if all_signals:
        files_to_send.append(("strategy_comparison.csv", f"Strateji Backtest | {len(all_signals)} sinyal"))
    if ll_details:
        files_to_send.append(("lead_lag_results.csv", f"Lead-Lag Detay | {len(ll_details)} takip olayi"))
    if ll_sum_path:
        files_to_send.append((ll_sum_path, f"Lead-Lag Ozet | {len(ll_summary)} lider"))

    for filepath, caption in files_to_send:
        send_telegram_document(filepath, caption)

    # ═══════════════════════════════════════════════════════════════
    # DATA ANALIZI (Pandas ile istatistiksel ozet)
    # ═══════════════════════════════════════════════════════════════
    print("\n" + "=" * 100)
    print("DATA ANALIZI - ISTATISTIKSEL OZET")
    print("=" * 100)

    if all_signals:
        df_sig = pd.DataFrame([
            {"strategy": s.strategy, "symbol": s.symbol, "direction": s.direction,
             "max_fav": s.max_favorable_pct, "hit_5": s.hit_targets.get(5),
             "hit_10": s.hit_targets.get(10), "hit_20": s.hit_targets.get(20)}
            for s in all_signals
        ])
        print("\n--- STRATEJI ISTATISTIKLERI ---")
        print(df_sig.groupby("strategy")["max_fav"].describe().round(2))
        print("\n--- YON BAZINDA ORTALAMA MAX FAV ---")
        print(df_sig.groupby(["strategy", "direction"])["max_fav"].mean().round(2))

    if ll_details:
        df_ll = pd.DataFrame(ll_details)
        print("\n--- LEAD-LAG ISTATISTIKLERI ---")
        print(f"Toplam takip olayi: {len(df_ll)}")
        print(f"Ortalama gecikme: {df_ll['lag_mins'].mean():.1f} dk")
        print(f"Median gecikme: {df_ll['lag_mins'].median():.1f} dk")
        print("\nEn cok liderlik edenler (takip olayi sayisi):")
        print(df_ll["leader"].value_counts().head(10))
        print("\nEn cok takip edenler:")
        print(df_ll["follower"].value_counts().head(10))
        print("\nYon bazinda dagilim:")
        print(df_ll["direction"].value_counts())

    print("\n" + "=" * 100)
    print("ISLEM TAMAMLANDI")
    print("=" * 100)
    print("CSV dosyalari:")
    for f, _ in files_to_send:
        print(f"  - {f}")
    print("\nTelegram gonderimi icin ortam degiskenleri:")
    print("  export TELEGRAM_TOKEN='123456:ABC...'")
    print("  export TELEGRAM_CHAT_ID='-1001234567890'")


if __name__ == "__main__":
    main()
