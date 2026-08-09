# -*- coding: utf-8 -*-
"""
MIKRO-GIRIS (1DK DIP + DONUS TEYIDI) BACKTEST
================================================
Onceki 15dk direnc kirilim backtest'inde bulunan sinyalleri girdi olarak alir.
Her sinyal icin: 15dk kirilim mumu KAPANDIKTAN sonraki dakikalari (1dk mum)
izler, "birkac kirmizi mum -> hacimli yesil donus" paternini arar ve varsa
o noktadan itibaren yeni bir giris fiyati/zamani belirler.

Sonra bu YENI giris noktasindan +%7 / +%20 hedeflerine, orijinal direnc
seviyesi stop olacak sekilde, tekrar simulasyon yapar. Boylece:
  - Bu mikro-giris paterni ne siklikla gerceklesiyor?
  - Gerceklestiginde basari orani orijinal (direkt) giristen daha mi iyi?
sorularina veriyle cevap verir.

Girdi: backtest_signals.csv (onceki scriptin uretttigi dosya)
Cikti: micro_entry_results.csv + ozet

ONEMLI: Binance API'ye (fapi.binance.com) canli ag gerektirir, Render'da
botun calistigi ortamda calistir.

Kullanim:
    python micro_entry_backtest.py
"""
import os
import time
import logging
import threading
from datetime import timedelta
from dataclasses import dataclass, asdict

import requests
import pandas as pd
import numpy as np

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger(__name__)

BINANCE_BASE = "https://fapi.binance.com"
REQUEST_TIMEOUT = 10
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
                log.warning(f"Rate limit, {wait:.0f}sn bekleniyor...")
                time.sleep(wait)
                continue
            r.raise_for_status()
            return r.json()
        except Exception as e:
            if attempt == retries - 1:
                log.error(f"Istek basarisiz: {e}")
                return None
            time.sleep(3 * (attempt + 1))
    return None


def get_klines_range(symbol, interval, start_ms, end_ms, limit=1500):
    all_rows = []
    cur = start_ms
    guard = 0
    while cur < end_ms and guard < 20:
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
# AYARLAR
# ══════════════════════════════════════════════════════════════════
WATCH_WINDOW_MIN = int(os.getenv("WATCH_WINDOW_MIN", "20"))
MIN_RED_STREAK = int(os.getenv("MIN_RED_STREAK", "2"))
REVERSAL_VOL_MULT = float(os.getenv("REVERSAL_VOL_MULT", "1.3"))

TARGETS_PCT = [7.0, 20.0]
FORWARD_MAX_BARS_15M = int(os.getenv("FORWARD_MAX_BARS_15M", "192"))  # 48 saat

SIGNALS_CSV = os.getenv("SIGNALS_CSV", "backtest_signals.csv")
OUTPUT_CSV = os.getenv("OUTPUT_CSV", "micro_entry_results.csv")


@dataclass
class MicroResult:
    symbol: str
    signal_time: str
    pattern_found: bool
    invalidated_before_entry: bool
    entry_time_new: object
    entry_price_new: object
    minutes_to_entry: object
    price_improvement_pct: object   # negatif = daha ucuza aldin (iyi), pozitif = daha pahaliya
    outcome_7pct_new: str
    outcome_20pct_new: str
    # karsilastirma icin orijinal (direkt giris) sonucu
    outcome_7pct_orig: str
    outcome_20pct_orig: str


def find_micro_entry(symbol, breakout_close_time, orig_entry_price, resistance):
    """15dk kirilim mumu kapandiktan sonraki WATCH_WINDOW_MIN dakikayi 1dk
    mumlarla tarar, dip+donus paternini arar."""
    start_ms = int(breakout_close_time.timestamp() * 1000)
    end_ms = int((breakout_close_time + timedelta(minutes=WATCH_WINDOW_MIN)).timestamp() * 1000)

    df1m = get_klines_range(symbol, "1m", start_ms, end_ms)
    if df1m is None or len(df1m) < MIN_RED_STREAK + 1:
        return None  # veri yok

    red_streak = 0
    dip_vols = []
    last_red_high = None    # bir onceki kirmizi mumun tepesi -> donus teyidi bunun ustune kapanmali
    invalidated = False

    for _, row in df1m.iterrows():
        # once gecersizlik kontrolu: kirilan direncin altina dusme
        if row["low"] <= resistance:
            invalidated = True
            break

        is_red = row["close"] < row["open"]
        if is_red:
            red_streak += 1
            dip_vols.append(row["volume"])
            last_red_high = row["high"]
        elif red_streak >= MIN_RED_STREAK and not is_red:
            # potansiyel donus mumu: bir onceki kirmizi mumun tepesini kirmali (erken teyit)
            avg_dip_vol = np.mean(dip_vols) if dip_vols else 0
            vol_ok = avg_dip_vol > 0 and row["volume"] >= REVERSAL_VOL_MULT * avg_dip_vol
            reclaim_ok = last_red_high is not None and row["close"] > last_red_high
            if vol_ok and reclaim_ok:
                return {
                    "entry_time": row["open_time"],
                    "entry_price": float(row["close"]),
                    "invalidated": False,
                }
            else:
                # donus teyidi zayif, dip serisini sifirla (baska bir dip beklenebilir)
                red_streak = 0
                dip_vols = []
                last_red_high = None
        else:
            # yesil mum ama henuz yeterli kirmizi seri yok -> sifirla
            red_streak = 0
            dip_vols = []
            last_red_high = None

    if invalidated:
        return {"entry_time": None, "entry_price": None, "invalidated": True}
    return None  # pattern hic olusmadi (timeout)


def simulate_forward(symbol, entry_time, entry_price, resistance):
    """Yeni giris noktasindan itibaren 15dk mumlarla +%7/+%20 hedef ve stop simulasyonu."""
    start_ms = int(entry_time.timestamp() * 1000)
    end_ms = int((entry_time + timedelta(minutes=15 * FORWARD_MAX_BARS_15M)).timestamp() * 1000)
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


def main():
    if not os.path.exists(SIGNALS_CSV):
        log.error(f"{SIGNALS_CSV} bulunamadi. Once 15dk backtest scriptini calistir.")
        return

    signals = pd.read_csv(SIGNALS_CSV)
    signals["signal_time"] = pd.to_datetime(signals["signal_time"], utc=True)
    log.info(f"{len(signals)} sinyal yuklendi. Mikro-giris analizi basliyor...")
    log.info(f"Ayarlar: watch={WATCH_WINDOW_MIN}dk min_red_streak={MIN_RED_STREAK} vol_mult={REVERSAL_VOL_MULT}")

    results = []
    for i, sig in signals.iterrows():
        symbol = sig["symbol"]
        # signal_time = 15dk kirilim mumunun ACILIS zamani -> kapanis = +15dk
        breakout_close_time = sig["signal_time"] + timedelta(minutes=15)
        resistance = float(sig["resistance"])
        orig_entry = float(sig["entry_price"])

        found = find_micro_entry(symbol, breakout_close_time, orig_entry, resistance)

        if found is None:
            results.append(MicroResult(
                symbol=symbol, signal_time=str(sig["signal_time"]),
                pattern_found=False, invalidated_before_entry=False,
                entry_time_new=None, entry_price_new=None, minutes_to_entry=None,
                price_improvement_pct=None,
                outcome_7pct_new="NO_PATTERN", outcome_20pct_new="NO_PATTERN",
                outcome_7pct_orig=sig["outcome_7pct"], outcome_20pct_orig=sig["outcome_20pct"],
            ))
        elif found["invalidated"]:
            results.append(MicroResult(
                symbol=symbol, signal_time=str(sig["signal_time"]),
                pattern_found=False, invalidated_before_entry=True,
                entry_time_new=None, entry_price_new=None, minutes_to_entry=None,
                price_improvement_pct=None,
                outcome_7pct_new="INVALIDATED", outcome_20pct_new="INVALIDATED",
                outcome_7pct_orig=sig["outcome_7pct"], outcome_20pct_orig=sig["outcome_20pct"],
            ))
        else:
            entry_time_new = found["entry_time"]
            entry_price_new = found["entry_price"]
            minutes_to_entry = (entry_time_new - breakout_close_time).total_seconds() / 60
            price_improvement_pct = (entry_price_new - orig_entry) / orig_entry * 100

            outcome_new = simulate_forward(symbol, entry_time_new, entry_price_new, resistance)

            results.append(MicroResult(
                symbol=symbol, signal_time=str(sig["signal_time"]),
                pattern_found=True, invalidated_before_entry=False,
                entry_time_new=str(entry_time_new), entry_price_new=entry_price_new,
                minutes_to_entry=minutes_to_entry,
                price_improvement_pct=price_improvement_pct,
                outcome_7pct_new=outcome_new[7.0], outcome_20pct_new=outcome_new[20.0],
                outcome_7pct_orig=sig["outcome_7pct"], outcome_20pct_orig=sig["outcome_20pct"],
            ))

        if (i + 1) % 10 == 0 or (i + 1) == len(signals):
            log.info(f"[{i+1}/{len(signals)}] islendi")

    df = pd.DataFrame([asdict(r) for r in results])
    df.to_csv(OUTPUT_CSV, index=False)
    log.info(f"Sonuc CSV yazildi: {OUTPUT_CSV}")

    # --- OZET ---
    n = len(df)
    n_pattern = (df["pattern_found"] == True).sum()          # noqa: E712
    n_invalid = (df["invalidated_before_entry"] == True).sum()  # noqa: E712
    n_timeout = n - n_pattern - n_invalid

    print("\n" + "=" * 70)
    print("MIKRO-GIRIS (1DK DIP+DONUS) OZET")
    print("=" * 70)
    print(f"Toplam sinyal: {n}")
    print(f"Pattern olustu (dip+donus tespit edildi): {n_pattern} (%{n_pattern/n*100:.1f})")
    print(f"Gecersiz oldu (giris oncesi stop'a gitti): {n_invalid} (%{n_invalid/n*100:.1f})")
    print(f"Pattern hic olusmadi (timeout, {WATCH_WINDOW_MIN}dk icinde ne dip ne donus): {n_timeout} (%{n_timeout/n*100:.1f})")

    pattern_df = df[df["pattern_found"] == True]  # noqa: E712
    if len(pattern_df) > 0:
        succ7_new = (pattern_df["outcome_7pct_new"] == "SUCCESS").mean() * 100
        succ20_new = (pattern_df["outcome_20pct_new"] == "SUCCESS").mean() * 100
        avg_improve = pattern_df["price_improvement_pct"].mean()
        avg_minutes = pattern_df["minutes_to_entry"].mean()
        print(f"\n--- Pattern olusan sinyallerde (n={len(pattern_df)}) ---")
        print(f"YENI giris basari (+%7): {succ7_new:.1f}%")
        print(f"YENI giris basari (+%20): {succ20_new:.1f}%")
        print(f"Ortalama fiyat farki (orijinal girise gore): %{avg_improve:.2f} (negatif=daha ucuz)")
        print(f"Ortalama giris gecikmesi: {avg_minutes:.1f} dakika")

        # ayni alt kumede ORIJINAL (direkt) giris basarisi (adil karsilastirma)
        succ7_orig = (pattern_df["outcome_7pct_orig"] == "SUCCESS").mean() * 100
        succ20_orig = (pattern_df["outcome_20pct_orig"] == "SUCCESS").mean() * 100
        print(f"\n--- AYNI sinyal grubunda ORIJINAL (direkt) giris basarisi (karsilastirma) ---")
        print(f"ORIJINAL giris basari (+%7): {succ7_orig:.1f}%")
        print(f"ORIJINAL giris basari (+%20): {succ20_orig:.1f}%")

    print("=" * 70)

    log.info("Tamamlandi. Sonsuz bekleme moduna geciliyor (Render restart-loop onlemi).")
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    main()
