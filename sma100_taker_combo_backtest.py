# -*- coding: utf-8 -*-
"""
BIRLESIK STRATEJI: SMA100 HIZLI KIRILIM + TAKER IMBALANCE (15dk)

En iyi cikan iki stratejiyi birlestirir:
  1) SMA100 Hizli Kirilim: fiyat SMA100'e yakinken (son 2-3 mumda uzaklik
     <=%1.5) aniden %4+ uzaklasiyor (kirilim mumu)
  2) Taker Imbalance: kirilim mumunun KENDISINDE taker buy/sell orani
     belirli bir esigi (0/%50/%55/%60/%65/%70) geciyor mu

MANTIK: SMA100 kirilimi zaten guclu bir sinyal (%48.2 hit-rate, 1055
sinyal). Soru: kirilim mumunda ayrica taker akisi da o yonde baskinsa
(LONG kiriliminda taker buy baskin, SHORT kiriliminda taker sell baskin),
isabet orani daha da artiyor mu?

BASELINE (taker esigi 0) = saf SMA100 kirilimi (taker filtresi yok).
Esik yukseldikce sinyal sayisi azalir, hit-rate degisimi izlenir.

ZAMAN DILIMI: 15dk | COIN: 200 (min 3M USDT) | LOOKBACK: 30 gun
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
TIMEFRAME = "15m"
TOP_N_SYMBOLS = 200
MIN_QUOTE_VOLUME_24H = 3_000_000
LOOKBACK_DAYS = 30

SMA_PERIOD = 100
NEAR_TOL_PCT = 1.5
BREAK_PCT = 4.0
LOOKBACK_WINDOW = 3

# Test edilecek taker imbalance esikleri (0 = filtre yok / baseline)
TAKER_THRESHOLDS = [0.0, 0.50, 0.55, 0.60, 0.65, 0.70]

FORWARD_HOURS = 48
TARGET_PCTS = [5, 10, 20]

TF_MINUTES = 15

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
    interval_ms = TF_MINUTES * 60 * 1000
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
    for c in ["open", "high", "low", "close", "volume", "taker_buy_base"]:
        df[c] = df[c].astype(float)
    df = df.drop_duplicates(subset="open_time").reset_index(drop=True)
    return df


# ══════════════════════════════════════════════════════════════════
# SINYAL
# ══════════════════════════════════════════════════════════════════
@dataclass
class Signal:
    symbol: str
    direction: str
    signal_time: str
    distance_pct: float
    taker_ratio: float      # yon ile uyumlu taker orani (LONG->buy, SHORT->sell)
    max_favorable_pct: float
    hit_targets: dict = field(default_factory=dict)


def compute_signals(symbol, df, forward_candles):
    signals = []
    n = len(df)
    min_needed = SMA_PERIOD + LOOKBACK_WINDOW + forward_candles + 5
    if n < min_needed:
        return signals

    close = df["close"].astype(float)
    sma100 = close.rolling(SMA_PERIOD).mean()
    distance_pct = (close - sma100) / sma100 * 100

    vol_safe = df["volume"].replace(0, np.nan).astype(float)
    buy_ratio = (df["taker_buy_base"].astype(float) / vol_safe)

    for i in range(SMA_PERIOD + LOOKBACK_WINDOW, n - forward_candles):
        d_now = distance_pct.iloc[i]
        if pd.isna(d_now):
            continue

        recent = distance_pct.iloc[i - LOOKBACK_WINDOW:i]
        if recent.isna().any():
            continue
        was_near = recent.abs().min() <= NEAR_TOL_PCT

        direction = None
        if was_near and d_now >= BREAK_PCT:
            direction = "LONG"
        elif was_near and d_now <= -BREAK_PCT:
            direction = "SHORT"
        if direction is None:
            continue

        d_prev = distance_pct.iloc[i - 1]
        if not pd.isna(d_prev):
            if direction == "LONG" and d_prev >= BREAK_PCT:
                continue
            if direction == "SHORT" and d_prev <= -BREAK_PCT:
                continue

        br = buy_ratio.iloc[i]
        if pd.isna(br):
            continue
        sr = 1.0 - br
        taker_ratio = br if direction == "LONG" else sr

        entry_price = float(close.iloc[i])
        future = df.iloc[i + 1: i + 1 + forward_candles]
        if len(future) == 0:
            continue
        if direction == "LONG":
            max_fav = (future["high"].max() - entry_price) / entry_price * 100
        else:
            max_fav = (entry_price - future["low"].min()) / entry_price * 100
        hit = {t: bool(max_fav >= t) for t in TARGET_PCTS}

        signals.append(Signal(
            symbol=symbol, direction=direction,
            signal_time=datetime.fromtimestamp(int(df["open_time"].iloc[i]) / 1000).strftime("%Y-%m-%d %H:%M"),
            distance_pct=float(d_now), taker_ratio=float(taker_ratio),
            max_favorable_pct=float(max_fav), hit_targets=hit,
        ))
    return signals


# ══════════════════════════════════════════════════════════════════
# RAPOR
# ══════════════════════════════════════════════════════════════════
def summarize(all_signals):
    print("\n" + "=" * 95)
    print(f"BIRLESIK: SMA100 KIRILIM + TAKER IMBALANCE (15dk)  {TOP_N_SYMBOLS} coin, {LOOKBACK_DAYS} gun")
    print(f"kirilim>=%{BREAK_PCT}, yakinlik<=%{NEAR_TOL_PCT} | taker esigi taraniyor")
    print("=" * 95)

    header = f"{'Taker esik':>11} | {'n':>6} |" + "".join(f" %{t} hit |" for t in TARGET_PCTS) + f" {'Ort.max.fav%':>13}"
    print(header)
    print("-" * len(header))

    for th in TAKER_THRESHOLDS:
        subset = [s for s in all_signals if s.taker_ratio >= th]
        n = len(subset)
        rates = {}
        for t in TARGET_PCTS:
            hits = sum(1 for s in subset if s.hit_targets.get(t))
            rates[t] = (hits / n * 100) if n else 0
        avg_fav = (sum(s.max_favorable_pct for s in subset) / n) if n else 0
        row = f"{th*100:>10.0f}% | {n:>6} |"
        for t in TARGET_PCTS:
            row += f" {rates[t]:>7.1f}% |"
        row += f" {avg_fav:>12.2f}%"
        print(row)

    print("\nYorum: '0%' satiri saf SMA100 kirilimi (taker filtresi yok - baseline, 1055 sinyalle")
    print("onceki testte %10 hit-rate %48.2 idi). Esik yukseldikce hit-rate baseline'in USTUNE")
    print("cikiyorsa, taker akisi eklemek gercekten degerlidir. Cikmiyorsa/dususe geciyorsa,")
    print("SMA100 kirilimi zaten kendi basina yeterince guclu bir filtre demektir.")

    # yon bazinda kirilim, esik=0 (baseline) ve esik=0.60 (tipik pratik esik) icin
    print("\n--- YON BAZINDA KIRILIM (taker esigine gore) ---")
    for th in TAKER_THRESHOLDS:
        for direction in ("LONG", "SHORT"):
            subset = [s for s in all_signals if s.taker_ratio >= th and s.direction == direction]
            if not subset:
                continue
            hits10 = sum(1 for s in subset if s.hit_targets.get(10))
            rate10 = hits10 / len(subset) * 100
            print(f"  taker>=%{th*100:.0f} {direction:<6} n={len(subset):>5}  %10 hit-rate={rate10:.1f}%")

    print("\n--- EN COK SINYAL URETEN 15 COIN (taker filtresiz, tum sinyaller) ---")
    from collections import Counter
    counts = Counter(s.symbol for s in all_signals)
    for sym, cnt in counts.most_common(15):
        sub = [s for s in all_signals if s.symbol == sym]
        hits10 = sum(1 for s in sub if s.hit_targets.get(10))
        print(f"  {sym:<15} n={cnt:>3}  %10 hit-rate={hits10/cnt*100:.1f}%")


def save_csv(all_signals, path="sma100_taker_combo_results.csv"):
    rows = []
    for s in all_signals:
        row = {
            "symbol": s.symbol, "direction": s.direction, "signal_time": s.signal_time,
            "distance_pct": round(s.distance_pct, 2), "taker_ratio": round(s.taker_ratio, 4),
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


def send_telegram_message(text):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return False
    try:
        r = session.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            data={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"},
            timeout=15,
        )
        return r.status_code == 200
    except Exception as e:
        log.error(f"Telegram mesaj hatasi: {e}")
        return False


# ══════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════
def main():
    log.info(f"Top {TOP_N_SYMBOLS} likit coin cekiliyor (min {MIN_QUOTE_VOLUME_24H/1e6:.1f}M USDT)...")
    symbols = get_top_symbols(TOP_N_SYMBOLS, MIN_QUOTE_VOLUME_24H)
    log.info(f"{len(symbols)} coin bulundu. TF={TIMEFRAME}, Lookback={LOOKBACK_DAYS}gun, "
              f"SMA{SMA_PERIOD} kirilim>=%{BREAK_PCT} + taker esik taramasi")

    forward_candles = int(FORWARD_HOURS * 60 / TF_MINUTES)
    end_ms = int(time.time() * 1000)
    start_ms = int((datetime.now() - timedelta(days=LOOKBACK_DAYS)).timestamp() * 1000)

    all_signals = []
    for idx, symbol in enumerate(symbols, 1):
        try:
            df = get_klines_range(symbol, TIMEFRAME, start_ms, end_ms)
            if df is None or len(df) < SMA_PERIOD + LOOKBACK_WINDOW + forward_candles + 5:
                continue
            sigs = compute_signals(symbol, df, forward_candles)
            all_signals.extend(sigs)
            if idx % 20 == 0:
                log.info(f"[{idx}/{len(symbols)}] islendi, su ana kadar {len(all_signals)} sinyal")
        except Exception as e:
            log.warning(f"{symbol} hata: {e}")
        time.sleep(0.05)

    if not all_signals:
        log.error("Hic sinyal bulunamadi.")
        return

    summarize(all_signals)
    save_csv(all_signals)

    if TELEGRAM_TOKEN and TELEGRAM_CHAT_ID:
        baseline = all_signals
        n0 = len(baseline)
        hits10_0 = sum(1 for s in baseline if s.hit_targets.get(10))
        best_th, best_rate, best_n = 0.0, hits10_0 / n0 * 100 if n0 else 0, n0
        for th in TAKER_THRESHOLDS:
            subset = [s for s in all_signals if s.taker_ratio >= th]
            if len(subset) < 30:
                continue
            hits = sum(1 for s in subset if s.hit_targets.get(10))
            rate = hits / len(subset) * 100
            if rate > best_rate:
                best_th, best_rate, best_n = th, rate, len(subset)

        msg = (
            "📊 <b>SMA100 KIRILIM + TAKER IMBALANCE BACKTEST</b>\n"
            f"{TOP_N_SYMBOLS} coin | {LOOKBACK_DAYS} gun | 15dk\n"
            "=" * 25 + "\n"
            f"Baseline (taker filtresiz): n={n0}, %10 hit-rate={hits10_0/n0*100:.1f}%\n"
            f"En iyi (n>=30 sartiyla): taker>=%{best_th*100:.0f} → n={best_n}, %10 hit-rate={best_rate:.1f}%\n"
            f"⏰ {datetime.now().strftime('%H:%M:%S %d/%m/%Y')}"
        )
        send_telegram_message(msg)
        sent = send_telegram_document("sma100_taker_combo_results.csv", caption="SMA100+Taker birlesik strateji - detayli sonuclar")
        if sent:
            log.info("Ozet ve CSV Telegram'a gonderildi.")


if __name__ == "__main__":
    main()
