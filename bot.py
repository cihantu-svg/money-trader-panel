# -*- coding: utf-8 -*-
"""
TAKER IMBALANCE BACKTEST - Seviye Bazli vs Delta (Ani Degisim) Bazli Karsilastirma

AMAC:
Canli botta 30 sinyalden sadece 2-3 tanesinin +%20 yaptigi gozlemlendi.
Bu script gecmis veride iki yaklasimi karsilastirir:

  A) SEVIYE BAZLI (mevcut canli bot mantigi)
     Son mumun taker buy/sell orani >= IMBALANCE_RATIO VE govde >= MIN_BODY_PCT

  B) DELTA BAZLI (ani degisim - yeni test edilen fikir)
     Yukaridaki sarta EK olarak:
     Son mumun taker oranı, o coinin son LOOKBACK_CANDLES mumunun ORTALAMA
     taker oranindan en az DELTA_THRESHOLD puan sapmis olmali.
     (Yani coin "normalde" %50 civarinda islem gorurken aniden %75'e sicramis mi,
      yoksa zaten %75 civarinda dolasan bir coin mi - bunu ayirt eder)

Her iki grup icin de sinyal sonrasi N mum icindeki EN YUKSEK lehte hareketi
(max favorable excursion) olcup, farkli hedef esiklerine (%5/%10/%20) ulasma
oranini (hit-rate) karsilastirir.

CIKTI: konsola ozet tablo + backtest_results.csv (tum sinyallerin detayi)

NOT: Bu script Binance Futures REST API'sine dogrudan istek atar, agir olabilir
(cok coin x uzun lookback = cok istek). Once kucuk parametrelerle dene
(orn. TOP_N_SYMBOLS=50, LOOKBACK_DAYS=15) sonra buyut.
"""
import time
import logging
from datetime import datetime, timedelta
from dataclasses import dataclass, field

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
LOOKBACK_DAYS = 15          # kac gunluk gecmis veri cekilecek
TOP_N_SYMBOLS = 80          # 24s hacme gore en likit kac coin taranacak
MIN_QUOTE_VOLUME_24H = 5_000_000

# --- SEVIYE BAZLI (mevcut canli bot ile ayni) ---
IMBALANCE_RATIO = 0.55
MIN_BODY_PCT = 4.0

# --- DELTA (ani degisim) AYARLARI ---
DELTA_LOOKBACK_CANDLES = 12     # coinin "normal" oranini hesaplarken bakilacak mum sayisi (12x5dk=1 saat)
DELTA_THRESHOLD_LIST = [0, 5, 10, 15, 20]   # test edilecek delta esikleri (puan). 0 = delta filtresi yok (A ile ayni)

# --- SONUC OLCUM AYARLARI ---
FORWARD_CANDLES = 36            # sinyal sonrasi kac mum ileri bakilacak (36x5dk=3 saat)
TARGET_PCTS = [5, 10, 20]       # hangi hedef yuzdelere ulasma orani olculecek

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
    """Binance limiti 1500 mum/istek oldugu icin sayfalanarak ceker."""
    all_rows = []
    cursor = start_ms
    interval_ms = 5 * 60 * 1000  # sadece 5m icin, farkli TF kullanirsan degistir
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
        time.sleep(0.1)  # nazik ol, rate limite takilma

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
# SINYAL + SONUC HESABI
# ══════════════════════════════════════════════════════════════════
@dataclass
class BacktestSignal:
    symbol: str
    direction: str
    signal_time: str
    entry_price: float
    body_pct: float
    taker_ratio: float
    rolling_avg_ratio: float
    delta: float             # taker_ratio ile rolling_avg arasindaki fark (puan, LONG icin +, SHORT icin de + olacak sekilde normalize)
    max_favorable_pct: float # sinyal sonrasi FORWARD_CANDLES icinde en iyi lehte hareket (%)
    hit_targets: dict = field(default_factory=dict)  # {5: True/False, 10: True/False, 20: True/False}


def compute_signals_for_symbol(symbol, df):
    """df: tum gecmis kapanmis mumlar (kronolojik sirada)."""
    signals = []
    n = len(df)
    if n < DELTA_LOOKBACK_CANDLES + FORWARD_CANDLES + 5:
        return signals

    buy_ratio = df["taker_buy_base"] / df["volume"].replace(0, pd.NA)
    body_pct = (df["close"] - df["open"]) / df["open"] * 100
    rolling_avg = buy_ratio.rolling(DELTA_LOOKBACK_CANDLES).mean().shift(1)  # kendisi haric onceki N mum

    for i in range(DELTA_LOOKBACK_CANDLES, n - FORWARD_CANDLES):
        br = buy_ratio.iloc[i]
        bp = body_pct.iloc[i]
        if pd.isna(br) or pd.isna(bp):
            continue
        sr = 1.0 - br
        avg = rolling_avg.iloc[i]
        if pd.isna(avg):
            continue

        direction = None
        delta = None
        if br >= IMBALANCE_RATIO and bp >= MIN_BODY_PCT:
            direction = "LONG"
            delta = (br - avg) * 100  # puan cinsinden
        elif sr >= IMBALANCE_RATIO and bp <= -MIN_BODY_PCT:
            direction = "SHORT"
            delta = ((1 - avg) - sr) * -1 * 100  # SHORT icin: sell orani normale gore ne kadar sicramis
            delta = (sr - (1 - avg)) * 100

        if direction is None:
            continue

        entry_price = float(df["close"].iloc[i])
        future = df.iloc[i + 1: i + 1 + FORWARD_CANDLES]
        if direction == "LONG":
            max_fav = (future["high"].max() - entry_price) / entry_price * 100
        else:
            max_fav = (entry_price - future["low"].min()) / entry_price * 100

        hit = {t: bool(max_fav >= t) for t in TARGET_PCTS}

        signals.append(BacktestSignal(
            symbol=symbol,
            direction=direction,
            signal_time=datetime.fromtimestamp(int(df["open_time"].iloc[i]) / 1000).strftime("%Y-%m-%d %H:%M"),
            entry_price=entry_price,
            body_pct=float(bp),
            taker_ratio=float(br if direction == "LONG" else sr),
            rolling_avg_ratio=float(avg if direction == "LONG" else (1 - avg)),
            delta=float(delta),
            max_favorable_pct=float(max_fav),
            hit_targets=hit,
        ))
    return signals


# ══════════════════════════════════════════════════════════════════
# ANALIZ / RAPOR
# ══════════════════════════════════════════════════════════════════
def summarize(signals):
    print("\n" + "=" * 70)
    print(f"TOPLAM SINYAL (delta filtresi yok - mevcut canli bot mantigi): {len(signals)}")
    print("=" * 70)

    for target in TARGET_PCTS:
        hits = sum(1 for s in signals if s.hit_targets.get(target))
        rate = (hits / len(signals) * 100) if signals else 0
        print(f"  >= %{target} hedefe ulasma orani: {hits}/{len(signals)}  (%{rate:.1f})")

    print("\n--- DELTA ESIGINE GORE ISABET ORANI (sadece bu delta ve uzeri sinyaller) ---")
    header = f"{'Delta esigi':>12} | {'Sinyal sayisi':>14} |" + "".join(f" %{t} hit-rate |" for t in TARGET_PCTS)
    print(header)
    print("-" * len(header))
    for th in DELTA_THRESHOLD_LIST:
        subset = [s for s in signals if s.delta >= th]
        row = f"{th:>11}p | {len(subset):>14} |"
        for t in TARGET_PCTS:
            hits = sum(1 for s in subset if s.hit_targets.get(t))
            rate = (hits / len(subset) * 100) if subset else 0
            row += f" {rate:>9.1f}% |"
        print(row)

    print("\nYorum: Delta esigi artikca sinyal sayisi azalir ama hit-rate yukseliyorsa,")
    print("'ani degisim' fikri gercekten isabeti artiriyor demektir. Hit-rate degismiyor")
    print("veya dususe geciyorsa, delta bu ozel senaryoda ayirt edici degildir.")


def save_csv(signals, path="backtest_results.csv"):
    rows = []
    for s in signals:
        row = {
            "symbol": s.symbol, "direction": s.direction, "signal_time": s.signal_time,
            "entry_price": s.entry_price, "body_pct": round(s.body_pct, 2),
            "taker_ratio": round(s.taker_ratio, 4), "rolling_avg_ratio": round(s.rolling_avg_ratio, 4),
            "delta_puan": round(s.delta, 2), "max_favorable_pct": round(s.max_favorable_pct, 2),
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
    log.info(f"{len(symbols)} coin bulundu. Backtest: son {LOOKBACK_DAYS} gun, TF={TIMEFRAME}")

    end_ms = int(time.time() * 1000)
    start_ms = int((datetime.now() - timedelta(days=LOOKBACK_DAYS)).timestamp() * 1000)

    all_signals = []
    for idx, symbol in enumerate(symbols, 1):
        try:
            df = get_klines_range(symbol, TIMEFRAME, start_ms, end_ms)
            if df is None or len(df) < 50:
                continue
            sigs = compute_signals_for_symbol(symbol, df)
            all_signals.extend(sigs)
            if idx % 10 == 0:
                log.info(f"[{idx}/{len(symbols)}] islendi, su ana kadar {len(all_signals)} sinyal")
        except Exception as e:
            log.warning(f"{symbol} hata: {e}")
        time.sleep(0.05)

    if not all_signals:
        log.error("Hic sinyal bulunamadi - parametreleri gevsetmeyi dene (IMBALANCE_RATIO/MIN_BODY_PCT).")
        return

    summarize(all_signals)
    save_csv(all_signals)


if __name__ == "__main__":
    main()
