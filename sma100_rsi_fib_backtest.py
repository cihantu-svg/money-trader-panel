# -*- coding: utf-8 -*-
"""
SMA100 KIRILIM + RSI(50) + FIBONACCI(%50) UC KATMANLI FILTRE BACKTEST

TEMEL SINYAL: SMA100 hizli kirilimi (once test edilen, %48.2 hit-rate,
1055 sinyal - degismedi).

EK FILTRELER (kirilim mumunda):
  RSI  : iki mod test edilir
    - CROSS : RSI(14) 50'yi TAM O MUMDA kesmis olmali (LONG: asagidan
              yukari, SHORT: yukaridan asagi)
    - SIDE  : RSI(14) sadece 50'nin dogru tarafinda olmali (kesisim sarti yok)

  FIBO : iki pencere test edilir (son 50 mum / son 100 mum, kirilim
         mumu HARIC - bakis onune gecmeyi (look-ahead) onlemek icin)
    - O penceredeki en yuksek/en dusuk nokta arasindaki %50 seviyesi
      hesaplanir (fib50 = (high+low)/2)
    - Fiyatin kirilim mumunda bu seviyeyi KESMIS olmasi aranir
      (LONG: bir onceki mum fib50 altinda, bu mum ustunde;
       SHORT: bir onceki mum fib50 ustunde, bu mum altinda)

KOMBINASYONLAR (2 RSI modu x 2 fib penceresi = 4 varyant), hepsi
BASELINE (filtresiz SMA100 kirilimi) ile karsilastirilir:
  1) RSI_CROSS + FIB50
  2) RSI_CROSS + FIB100
  3) RSI_SIDE  + FIB50
  4) RSI_SIDE  + FIB100

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

RSI_PERIOD = 14
FIB_WINDOWS = [50, 100]

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
# SINYAL
# ══════════════════════════════════════════════════════════════════
@dataclass
class Signal:
    symbol: str
    direction: str
    signal_time: str
    max_favorable_pct: float
    hit_targets: dict = field(default_factory=dict)
    rsi_now: float = None
    rsi_prev: float = None
    close_now: float = None
    close_prev: float = None
    fib50_50: float = None      # son 50 muma gore fib %50 seviyesi
    fib50_100: float = None     # son 100 muma gore fib %50 seviyesi


def rsi_cross_ok(s, direction):
    if s.rsi_now is None or s.rsi_prev is None:
        return False
    if direction == "LONG":
        return s.rsi_prev < 50 <= s.rsi_now
    return s.rsi_prev >= 50 > s.rsi_now


def rsi_side_ok(s, direction):
    if s.rsi_now is None:
        return False
    return s.rsi_now >= 50 if direction == "LONG" else s.rsi_now < 50


def fib_cross_ok(s, direction, window):
    level = s.fib50_50 if window == 50 else s.fib50_100
    if level is None or pd.isna(level) or s.close_now is None or s.close_prev is None:
        return False
    if direction == "LONG":
        return s.close_prev < level <= s.close_now
    return s.close_prev >= level > s.close_now


def compute_signals(symbol, df, forward_candles):
    signals = []
    n = len(df)
    min_needed = max(SMA_PERIOD, max(FIB_WINDOWS)) + LOOKBACK_WINDOW + forward_candles + 5
    if n < min_needed:
        return signals

    close = df["close"].astype(float)
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    sma100 = close.rolling(SMA_PERIOD).mean()
    distance_pct = (close - sma100) / sma100 * 100
    rsi = compute_rsi(close, RSI_PERIOD)

    roll_high_50 = high.rolling(50).max().shift(1)
    roll_low_50 = low.rolling(50).min().shift(1)
    fib50_50 = (roll_high_50 + roll_low_50) / 2

    roll_high_100 = high.rolling(100).max().shift(1)
    roll_low_100 = low.rolling(100).min().shift(1)
    fib50_100 = (roll_high_100 + roll_low_100) / 2

    start_i = max(SMA_PERIOD, max(FIB_WINDOWS)) + LOOKBACK_WINDOW
    for i in range(start_i, n - forward_candles):
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
            max_favorable_pct=float(max_fav), hit_targets=hit,
            rsi_now=float(rsi.iloc[i]) if not pd.isna(rsi.iloc[i]) else None,
            rsi_prev=float(rsi.iloc[i - 1]) if not pd.isna(rsi.iloc[i - 1]) else None,
            close_now=float(close.iloc[i]), close_prev=float(close.iloc[i - 1]),
            fib50_50=float(fib50_50.iloc[i]) if not pd.isna(fib50_50.iloc[i]) else None,
            fib50_100=float(fib50_100.iloc[i]) if not pd.isna(fib50_100.iloc[i]) else None,
        ))
    return signals


# ══════════════════════════════════════════════════════════════════
# RAPOR
# ══════════════════════════════════════════════════════════════════
VARIANTS = [
    ("RSI_CROSS + FIB50", lambda s, d: rsi_cross_ok(s, d) and fib_cross_ok(s, d, 50)),
    ("RSI_CROSS + FIB100", lambda s, d: rsi_cross_ok(s, d) and fib_cross_ok(s, d, 100)),
    ("RSI_SIDE + FIB50", lambda s, d: rsi_side_ok(s, d) and fib_cross_ok(s, d, 50)),
    ("RSI_SIDE + FIB100", lambda s, d: rsi_side_ok(s, d) and fib_cross_ok(s, d, 100)),
]


def summarize(all_signals):
    print("\n" + "=" * 100)
    print(f"SMA100 KIRILIM + RSI(50) + FIBONACCI(%50) FILTRE KARSILASTIRMA (15dk)")
    print(f"{TOP_N_SYMBOLS} coin, {LOOKBACK_DAYS} gun, kirilim>=%{BREAK_PCT}")
    print("=" * 100)

    def rates_for(subset):
        n = len(subset)
        rates = {}
        for t in TARGET_PCTS:
            hits = sum(1 for s in subset if s.hit_targets.get(t))
            rates[t] = (hits / n * 100) if n else 0
        avg_fav = (sum(s.max_favorable_pct for s in subset) / n) if n else 0
        return n, rates, avg_fav

    header = f"{'Varyant':<22} | {'n':>6} |" + "".join(f" %{t} hit |" for t in TARGET_PCTS) + f" {'Ort.max.fav%':>13}"
    print(header)
    print("-" * len(header))

    n0, rates0, fav0 = rates_for(all_signals)
    row = f"{'BASELINE (filtresiz)':<22} | {n0:>6} |"
    for t in TARGET_PCTS:
        row += f" {rates0[t]:>7.1f}% |"
    row += f" {fav0:>12.2f}%"
    print(row)
    print("-" * len(header))

    for name, cond in VARIANTS:
        subset = [s for s in all_signals if cond(s, s.direction)]
        n, rates, fav = rates_for(subset)
        row = f"{name:<22} | {n:>6} |"
        for t in TARGET_PCTS:
            row += f" {rates[t]:>7.1f}% |"
        row += f" {fav:>12.2f}%"
        print(row)

    print("\nYorum: BASELINE = saf SMA100 kirilimi (onceki testte %10 hit-rate %48.2, 1055 sinyal).")
    print("Herhangi bir varyant BASELINE'in USTUNE cikiyorsa, o RSI+Fib kombinasyonu gercekten")
    print("degerli bir ek filtredir. n kucukse (<30) rakamlara temkinli yaklas.")

    print("\n--- YON BAZINDA KIRILIM (her varyant, %10 hit-rate) ---")
    for name, cond in VARIANTS:
        for direction in ("LONG", "SHORT"):
            subset = [s for s in all_signals if s.direction == direction and cond(s, direction)]
            if not subset:
                continue
            hits = sum(1 for s in subset if s.hit_targets.get(10))
            rate = hits / len(subset) * 100
            print(f"  {name:<22} {direction:<6} n={len(subset):>5}  %10 hit-rate={rate:.1f}%")


def save_csv(all_signals, path="sma100_rsi_fib_results.csv"):
    rows = []
    for s in all_signals:
        row = {
            "symbol": s.symbol, "direction": s.direction, "signal_time": s.signal_time,
            "max_favorable_pct": round(s.max_favorable_pct, 2),
            "rsi_now": s.rsi_now, "rsi_prev": s.rsi_prev,
            "close_now": s.close_now, "close_prev": s.close_prev,
            "fib50_50": s.fib50_50, "fib50_100": s.fib50_100,
        }
        for t in TARGET_PCTS:
            row[f"hit_{t}pct"] = s.hit_targets.get(t)
        for name, cond in VARIANTS:
            row[name.replace(" ", "")] = bool(cond(s, s.direction))
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
    log.info(f"{len(symbols)} coin bulundu. SMA100 kirilim + RSI50 + Fib50 filtreleri test edilecek")

    forward_candles = int(FORWARD_HOURS * 60 / TF_MINUTES)
    end_ms = int(time.time() * 1000)
    start_ms = int((datetime.now() - timedelta(days=LOOKBACK_DAYS)).timestamp() * 1000)

    all_signals = []
    for idx, symbol in enumerate(symbols, 1):
        try:
            df = get_klines_range(symbol, TIMEFRAME, start_ms, end_ms)
            if df is None or len(df) < max(SMA_PERIOD, max(FIB_WINDOWS)) + LOOKBACK_WINDOW + forward_candles + 5:
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
        n0 = len(all_signals)
        hits10_0 = sum(1 for s in all_signals if s.hit_targets.get(10))
        lines = [f"Baseline: n={n0}, %10 hit-rate={hits10_0/n0*100:.1f}%"]
        for name, cond in VARIANTS:
            subset = [s for s in all_signals if cond(s, s.direction)]
            n = len(subset)
            if n == 0:
                lines.append(f"{name}: sinyal yok")
                continue
            hits = sum(1 for s in subset if s.hit_targets.get(10))
            lines.append(f"{name}: n={n}, %10 hit-rate={hits/n*100:.1f}%")

        msg = (
            "📊 <b>SMA100 + RSI50 + FIB50 FILTRE BACKTEST</b>\n"
            f"{TOP_N_SYMBOLS} coin | {LOOKBACK_DAYS} gun | 15dk\n"
            "=" * 25 + "\n"
            + "\n".join(lines)
            + f"\n\n⏰ {datetime.now().strftime('%H:%M:%S %d/%m/%Y')}"
        )
        send_telegram_message(msg)
        sent = send_telegram_document("sma100_rsi_fib_results.csv", caption="SMA100+RSI50+Fib50 - detayli sonuclar")
        if sent:
            log.info("Ozet ve CSV Telegram'a gonderildi.")


if __name__ == "__main__":
    main()
