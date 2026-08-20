# -*- coding: utf-8 -*-
"""
BIRLESIK BACKTEST: GOVDE ESIGI TARAMASI (%3-6) x LONG/SHORT RATIO (ONAY/FADE)

Veri BIR KEZ cekilir (150 coin x 15 gun 5dk mum), ustune:

  1) Taker imbalance %60 sabit, govde esigi %3.0'dan %6.0'a taranir
     (7 farkli govde esigi, her biri ayri sinyal seti uretir)
  2) Her govde esiginin urettigi sinyaller icin, sinyal ureten semboller
     bazinda Long/Short Account Ratio verisi cekilir (tekrarlanan sembollerde
     TEKRAR CEKILMEZ - cache'lenir, boylece L/S istek sayisi minimize edilir)
  3) Her govde esigi X her L/S esigi (0.55/0.60/0.65/0.70) icin:
       - BASELINE   : L/S filtresi yok
       - ONAY       : L/S sinyal yonuyle ayni tarafta olanlar
       - FADE       : L/S asiriysa yon tersine cevrilir (dogru forward-move
                       ile yeniden hesaplanir)

CIKTI: konsolda govde esigi x L/S esigi kirilimli buyuk karsilastirma
tablosu + combined_results.csv (tum sinyaller, tum parametrelerle)

UYARI: Bu en agir backtest - 150 coin x 7 govde esigi x L/S verisi.
Suresi onceki scriptlerden belirgin uzun olabilir, sabirli ol.
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
TOP_N_SYMBOLS = 150
MIN_QUOTE_VOLUME_24H = 5_000_000

IMBALANCE_RATIO = 0.60
MIN_BODY_PCT_LIST = [3.0, 3.5, 4.0, 4.5, 5.0, 5.5, 6.0]

LS_PERIOD = "5m"
LS_MERGE_TOLERANCE_MIN = 10
LS_THRESHOLDS = [0.55, 0.60, 0.65, 0.70]

FORWARD_CANDLES = 36
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


def get_ls_ratio_range(symbol, start_ms, end_ms):
    all_rows = []
    cursor = start_ms
    interval_ms = 5 * 60 * 1000
    while cursor < end_ms:
        try:
            r = _request_with_retry(
                f"{BINANCE_BASE}/futures/data/globalLongShortAccountRatio",
                params={"symbol": symbol, "period": LS_PERIOD, "startTime": cursor,
                        "endTime": end_ms, "limit": 500},
            )
        except Exception as e:
            log.debug(f"L/S ratio istek hatasi ({symbol}): {e}")
            return None
        raw = r.json()
        if not raw:
            break
        all_rows.extend(raw)
        last_ts = raw[-1]["timestamp"]
        next_cursor = last_ts + interval_ms
        if next_cursor <= cursor:
            break
        cursor = next_cursor
        if len(raw) < 500:
            break
        time.sleep(0.08)

    if not all_rows:
        return None
    df = pd.DataFrame(all_rows)
    if "timestamp" not in df.columns:
        return None
    df["timestamp"] = df["timestamp"].astype(np.int64)
    df["longAccount"] = df["longAccount"].astype(float)
    df["shortAccount"] = df["shortAccount"].astype(float)
    df = df.drop_duplicates(subset="timestamp").sort_values("timestamp").reset_index(drop=True)
    return df[["timestamp", "longAccount", "shortAccount"]]


# ══════════════════════════════════════════════════════════════════
# SINYAL YAPISI
# ══════════════════════════════════════════════════════════════════
@dataclass
class Signal:
    body_pct_threshold: float
    symbol: str
    direction: str
    signal_time_ms: int
    entry_idx: int          # df icindeki satir index'i (fade yeniden hesabi icin)
    max_favorable_pct: float
    hit_targets: dict = field(default_factory=dict)
    long_account: float = None


def compute_signals(symbol, df, min_body_pct):
    signals = []
    n = len(df)
    if n < FORWARD_CANDLES + 5:
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
        if br >= IMBALANCE_RATIO and bp >= min_body_pct:
            direction = "LONG"
        elif sr >= IMBALANCE_RATIO and bp <= -min_body_pct:
            direction = "SHORT"
        if direction is None:
            continue

        entry_price = float(close.iloc[i])
        future = df.iloc[i + 1: i + 1 + FORWARD_CANDLES]
        if direction == "LONG":
            max_fav = (future["high"].max() - entry_price) / entry_price * 100
        else:
            max_fav = (entry_price - future["low"].min()) / entry_price * 100
        hit = {t: bool(max_fav >= t) for t in TARGET_PCTS}

        signals.append(Signal(
            body_pct_threshold=min_body_pct, symbol=symbol, direction=direction,
            signal_time_ms=int(df["open_time"].iloc[i]), entry_idx=i,
            max_favorable_pct=float(max_fav), hit_targets=hit,
        ))
    return signals


def fade_forward_move(df, i, new_direction):
    entry_price = float(df["close"].iloc[i])
    future = df.iloc[i + 1: i + 1 + FORWARD_CANDLES]
    if new_direction == "LONG":
        max_fav = (future["high"].max() - entry_price) / entry_price * 100
    else:
        max_fav = (entry_price - future["low"].min()) / entry_price * 100
    hit = {t: bool(max_fav >= t) for t in TARGET_PCTS}
    return max_fav, hit


# ══════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════
def main():
    log.info(f"Top {TOP_N_SYMBOLS} likit coin cekiliyor (min {MIN_QUOTE_VOLUME_24H/1e6:.1f}M USDT)...")
    symbols = get_top_symbols(TOP_N_SYMBOLS, MIN_QUOTE_VOLUME_24H)
    log.info(f"{len(symbols)} coin bulundu. Govde esikleri: {MIN_BODY_PCT_LIST} | taker=%{IMBALANCE_RATIO*100:.0f}")

    end_ms = int(time.time() * 1000)
    start_ms = int((datetime.now() - timedelta(days=LOOKBACK_DAYS)).timestamp() * 1000)

    signals_by_threshold = {th: [] for th in MIN_BODY_PCT_LIST}
    df_by_symbol = {}

    for idx, symbol in enumerate(symbols, 1):
        try:
            df = get_klines_range(symbol, TIMEFRAME, start_ms, end_ms)
            if df is None or len(df) < FORWARD_CANDLES + 5:
                continue
            df_by_symbol[symbol] = df
            for th in MIN_BODY_PCT_LIST:
                signals_by_threshold[th].extend(compute_signals(symbol, df, th))
            if idx % 20 == 0:
                total = sum(len(v) for v in signals_by_threshold.values())
                log.info(f"[{idx}/{len(symbols)}] islendi, su ana kadar {total} toplam sinyal (tum esikler)")
        except Exception as e:
            log.warning(f"{symbol} kline hata: {e}")
        time.sleep(0.05)

    # tum esiklerdeki sinyal ureten sembollerin BIRLESIMI - L/S sadece bunlar icin cekilecek
    signal_symbols = set()
    for sigs in signals_by_threshold.values():
        signal_symbols.update(s.symbol for s in sigs)
    signal_symbols = sorted(signal_symbols)
    log.info(f"Toplam {sum(len(v) for v in signals_by_threshold.values())} sinyal, "
              f"{len(signal_symbols)} benzersiz sembol. L/S ratio verisi cekiliyor...")

    ls_cache = {}
    for idx, symbol in enumerate(signal_symbols, 1):
        try:
            ls_cache[symbol] = get_ls_ratio_range(symbol, start_ms, end_ms)
        except Exception as e:
            log.warning(f"{symbol} L/S hata: {e}")
            ls_cache[symbol] = None
        if idx % 20 == 0:
            log.info(f"[{idx}/{len(signal_symbols)}] L/S verisi cekildi")
        time.sleep(0.1)

    # L/S esleme (her esik grubu icin, ayni sembolun ayni ls_df'i tekrar kullanilir)
    def attach_ls(sigs):
        by_symbol = {}
        for s in sigs:
            by_symbol.setdefault(s.symbol, []).append(s)
        for symbol, group in by_symbol.items():
            ls_df = ls_cache.get(symbol)
            if ls_df is None or ls_df.empty:
                continue
            sig_df = pd.DataFrame({"idx": range(len(group)), "ts": [s.signal_time_ms for s in group]}).sort_values("ts")
            ls_sorted = ls_df.sort_values("timestamp")
            tol_ms = LS_MERGE_TOLERANCE_MIN * 60 * 1000
            merged = pd.merge_asof(sig_df, ls_sorted, left_on="ts", right_on="timestamp",
                                    direction="backward", tolerance=tol_ms)
            for _, row in merged.iterrows():
                group[int(row["idx"])].long_account = row["longAccount"] if pd.notna(row.get("longAccount")) else None

    for th in MIN_BODY_PCT_LIST:
        attach_ls(signals_by_threshold[th])

    # ══════════════════════════════════════════════════════════════
    # RAPOR
    # ══════════════════════════════════════════════════════════════
    print("\n" + "=" * 100)
    print(f"BIRLESIK SONUC: GOVDE ESIGI x L/S RATIO  ({TOP_N_SYMBOLS} coin, {LOOKBACK_DAYS} gun, taker>=%{IMBALANCE_RATIO*100:.0f})")
    print("=" * 100)

    all_rows_for_csv = []

    for th in MIN_BODY_PCT_LIST:
        sigs = signals_by_threshold[th]
        with_ls = [s for s in sigs if s.long_account is not None]
        print(f"\n{'#'*70}")
        print(f"GOVDE ESIGI >= %{th:.1f}   (toplam sinyal: {len(sigs)}, L/S eslesen: {len(with_ls)})")
        print(f"{'#'*70}")

        # BASELINE (govde esigi ozeti, L/S filtresiz - TUM sinyaller uzerinden, L/S sarti aranmiyor)
        n_all = len(sigs)
        rates_all = {}
        for t in TARGET_PCTS:
            hits = sum(1 for s in sigs if s.hit_targets.get(t))
            rates_all[t] = (hits / n_all * 100) if n_all else 0
        avg_fav_all = (sum(s.max_favorable_pct for s in sigs) / n_all) if n_all else 0
        print(f"  [BASELINE - tum sinyaller, L/S'siz]  n={n_all}  "
              + " ".join(f"%{t}={rates_all[t]:.1f}%" for t in TARGET_PCTS)
              + f"  ort.fav={avg_fav_all:.2f}%")

        if not with_ls:
            print("  (L/S verisi eslenen sinyal yok, onay/fade tablosu atlandi)")
            for s in sigs:
                all_rows_for_csv.append({
                    "body_threshold": th, "ls_mode": "baseline_no_ls", "ls_threshold": None,
                    "symbol": s.symbol, "direction": s.direction, "max_favorable_pct": round(s.max_favorable_pct, 2),
                    "hit_10pct": s.hit_targets.get(10),
                })
            continue

        # ONAY
        print(f"\n  [ONAY - L/S sinyal yonuyle ayni tarafta]")
        header = f"    {'L/S esik':>9} | {'n':>5} |" + "".join(f" %{t}|" for t in TARGET_PCTS) + " ort.fav%"
        print(header)
        for lth in LS_THRESHOLDS:
            kept = []
            for s in with_ls:
                la = s.long_account
                if s.direction == "LONG" and la >= lth:
                    kept.append(s)
                elif s.direction == "SHORT" and (1 - la) >= lth:
                    kept.append(s)
            n = len(kept)
            rates = {}
            for t in TARGET_PCTS:
                hits = sum(1 for s in kept if s.hit_targets.get(t))
                rates[t] = (hits / n * 100) if n else 0
            avg_fav = (sum(s.max_favorable_pct for s in kept) / n) if n else 0
            row = f"    {lth*100:>8.0f}% | {n:>5} |"
            for t in TARGET_PCTS:
                row += f" {rates[t]:>4.1f}|"
            row += f" {avg_fav:>7.2f}%"
            print(row)
            for s in kept:
                all_rows_for_csv.append({
                    "body_threshold": th, "ls_mode": "confirm", "ls_threshold": lth,
                    "symbol": s.symbol, "direction": s.direction, "max_favorable_pct": round(s.max_favorable_pct, 2),
                    "hit_10pct": s.hit_targets.get(10),
                })

        # FADE
        print(f"\n  [FADE - L/S asiriysa yon tersine cevrilir]")
        print(header)
        for lth in LS_THRESHOLDS:
            kept = []
            for s in with_ls:
                la = s.long_account
                df = df_by_symbol.get(s.symbol)
                if df is None:
                    continue
                if s.direction == "LONG" and la >= lth:
                    max_fav, hit = fade_forward_move(df, s.entry_idx, "SHORT")
                    kept.append((max_fav, hit))
                elif s.direction == "SHORT" and (1 - la) >= lth:
                    max_fav, hit = fade_forward_move(df, s.entry_idx, "LONG")
                    kept.append((max_fav, hit))
                else:
                    kept.append((s.max_favorable_pct, s.hit_targets))
            n = len(kept)
            rates = {}
            for t in TARGET_PCTS:
                hits = sum(1 for _, ht in kept if ht.get(t))
                rates[t] = (hits / n * 100) if n else 0
            avg_fav = (sum(f for f, _ in kept) / n) if n else 0
            row = f"    {lth*100:>8.0f}% | {n:>5} |"
            for t in TARGET_PCTS:
                row += f" {rates[t]:>4.1f}|"
            row += f" {avg_fav:>7.2f}%"
            print(row)

    print("\n" + "=" * 100)
    print("GENEL YORUM:")
    print("- Govde esigi dustukce sinyal sayisi artar, genelde hit-rate duser.")
    print("- ONAY tablosunda n azalirken hit-rate BASELINE'in ustune cikiyorsa L/S onayi degerlidir.")
    print("- FADE tablosunda n sabit kalir (sadece yon degisir); hit-rate baseline'dan yuksekse")
    print("  'kalabaligin tersine gitmek' bu strateji+esik kombinasyonunda isliyor demektir.")
    print("- En iyi kombinasyonu secerken hem n (>=30 tercih edilir) hem hit-rate'e beraber bak.")
    print("=" * 100)

    pd.DataFrame(all_rows_for_csv).to_csv("combined_results.csv", index=False)
    log.info("Detayli sonuclar kaydedildi: combined_results.csv")


if __name__ == "__main__":
    main()
