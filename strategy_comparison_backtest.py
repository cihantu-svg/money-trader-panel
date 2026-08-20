# -*- coding: utf-8 -*-
"""
STRATEJI B (SIKI IMBALANCE) + LONG/SHORT ACCOUNT RATIO BACKTEST

En iyi cikan strateji B'yi (taker >= %60 + govde >= %6) temel alip,
Binance'in globalLongShortAccountRatio verisiyle (hesap bazinda long/short
dagilimi) iki farkli mantigi test eder:

  1) ONAY (confirmation): L/S orani sinyal yonuyle AYNI tarafta ise
     (LONG sinyalinde longAccount >= esik, SHORT sinyalinde shortAccount >= esik)
     sinyal ONAYLANMIS sayilir, digerleri elenir.

  2) FADE (contrarian): L/S orani sinyal yonunde AŞIRI yigilmissa
     (kalabalik zaten o yonde asiri pozisyonlu -> tukenme riski),
     sinyal yonu TERSINE CEVRILIR (LONG->SHORT, SHORT->LONG).
     Asiri degilse sinyal oldugu gibi kalir.

Her iki mantik da birkac esik (0.55/0.60/0.65/0.70) ile taranir ve
BASELINE (L/S filtresi yok, sadece B stratejisi) ile karsilastirilir.

NOT: globalLongShortAccountRatio API'si Binance'de genelde ~30 gunluk
gecmis veri tutar. period=5m kullanilarak kline zamanlariyla hizalanir.
Sadece B stratejisinin sinyal urettigi semboller icin L/S verisi cekilir
(gereksiz istek yapilmasin diye).
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

# --- STRATEJI B (kazanan strateji) ---
B_IMBALANCE_RATIO = 0.60
B_MIN_BODY_PCT = 6.0

# --- L/S RATIO AYARLARI ---
LS_PERIOD = "5m"
LS_MERGE_TOLERANCE_MIN = 10   # sinyal zamanina en yakin L/S verisi bu dakika icinde olmali

# Test edilecek esikler (fraksiyon: 0.60 = %60)
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
    """globalLongShortAccountRatio - hesap bazinda long/short dagilimi.
    Binance genelde ~30 gunluk gecmis tutar, limit max 500/istek."""
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
# STRATEJI B SINYALLERI
# ══════════════════════════════════════════════════════════════════
@dataclass
class BSignal:
    symbol: str
    direction: str
    signal_time_ms: int
    signal_time: str
    entry_price: float
    max_favorable_pct: float
    hit_targets: dict = field(default_factory=dict)


def compute_strategy_b(symbol, df):
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
        if br >= B_IMBALANCE_RATIO and bp >= B_MIN_BODY_PCT:
            direction = "LONG"
        elif sr >= B_IMBALANCE_RATIO and bp <= -B_MIN_BODY_PCT:
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

        signals.append(BSignal(
            symbol=symbol, direction=direction,
            signal_time_ms=int(df["open_time"].iloc[i]),
            signal_time=datetime.fromtimestamp(int(df["open_time"].iloc[i]) / 1000).strftime("%Y-%m-%d %H:%M"),
            entry_price=entry_price, max_favorable_pct=float(max_fav), hit_targets=hit,
        ))
    return signals


# ══════════════════════════════════════════════════════════════════
# L/S ILE ESLESTIRME
# ══════════════════════════════════════════════════════════════════
def attach_ls_ratio(signals, ls_df):
    """Her sinyale, sinyal zamanina en yakin (geriye donuk) L/S degerini ekler."""
    if ls_df is None or ls_df.empty or not signals:
        for s in signals:
            s.__dict__["long_account"] = None
        return signals

    sig_df = pd.DataFrame({
        "idx": range(len(signals)),
        "ts": [s.signal_time_ms for s in signals],
    }).sort_values("ts")
    ls_sorted = ls_df.sort_values("timestamp")

    tol_ms = LS_MERGE_TOLERANCE_MIN * 60 * 1000
    merged = pd.merge_asof(
        sig_df, ls_sorted, left_on="ts", right_on="timestamp",
        direction="backward", tolerance=tol_ms,
    )

    for _, row in merged.iterrows():
        s = signals[int(row["idx"])]
        s.__dict__["long_account"] = row["longAccount"] if pd.notna(row.get("longAccount")) else None
    return signals


# ══════════════════════════════════════════════════════════════════
# VARYANT DEGERLENDIRME (baseline / onay - fade ayri, dogru forward-move
# hesaplamasi gerektirdigi icin main() icinde ozel olarak yapiliyor)
# ══════════════════════════════════════════════════════════════════
def eval_variant(signals, mode, threshold):
    """mode: 'baseline' | 'confirm'
    Dondurulen: (n, hit_rates dict, avg_fav)
    """
    kept = []
    for s in signals:
        la = getattr(s, "long_account", None)
        if la is None:
            continue  # L/S verisi yoksa bu sinyali degerlendirmeye katma (adil kiyaslama icin)

        if mode == "baseline":
            kept.append((s.hit_targets, s.max_favorable_pct))
            continue

        if mode == "confirm":
            if s.direction == "LONG" and la >= threshold:
                kept.append((s.hit_targets, s.max_favorable_pct))
            elif s.direction == "SHORT" and (1 - la) >= threshold:
                kept.append((s.hit_targets, s.max_favorable_pct))
            # aksi halde elenir

    n = len(kept)
    rates = {}
    for t in TARGET_PCTS:
        hits = sum(1 for ht, _ in kept if ht.get(t))
        rates[t] = (hits / n * 100) if n else 0
    avg_fav = (sum(f for _, f in kept) / n) if n else 0
    return n, rates, avg_fav


# ══════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════
def main():
    log.info(f"Top {TOP_N_SYMBOLS} likit coin cekiliyor (min {MIN_QUOTE_VOLUME_24H/1e6:.1f}M USDT)...")
    symbols = get_top_symbols(TOP_N_SYMBOLS, MIN_QUOTE_VOLUME_24H)
    log.info(f"{len(symbols)} coin bulundu. Once Strateji B sinyalleri hesaplanacak.")

    end_ms = int(time.time() * 1000)
    start_ms = int((datetime.now() - timedelta(days=LOOKBACK_DAYS)).timestamp() * 1000)

    all_signals = []
    # fade icin doğru forward-move hesaplamak uzere (symbol, df, signal, index) sakla
    signal_context = []

    for idx, symbol in enumerate(symbols, 1):
        try:
            df = get_klines_range(symbol, TIMEFRAME, start_ms, end_ms)
            if df is None or len(df) < FORWARD_CANDLES + 5:
                continue
            sigs = compute_strategy_b(symbol, df)
            if not sigs:
                continue
            all_signals.extend(sigs)
            for s in sigs:
                signal_context.append((symbol, df, s))
            if idx % 20 == 0:
                log.info(f"[{idx}/{len(symbols)}] islendi, su ana kadar {len(all_signals)} B sinyali")
        except Exception as e:
            log.warning(f"{symbol} kline hata: {e}")
        time.sleep(0.05)

    log.info(f"Toplam {len(all_signals)} Strateji B sinyali bulundu. Simdi L/S ratio verisi cekilecek "
              f"(sadece sinyal ureten {len(set(s.symbol for s in all_signals))} sembol icin)...")

    signal_symbols = sorted(set(s.symbol for s in all_signals))
    ls_cache = {}
    for idx, symbol in enumerate(signal_symbols, 1):
        try:
            ls_df = get_ls_ratio_range(symbol, start_ms, end_ms)
            ls_cache[symbol] = ls_df
        except Exception as e:
            log.warning(f"{symbol} L/S hata: {e}")
            ls_cache[symbol] = None
        if idx % 20 == 0:
            log.info(f"[{idx}/{len(signal_symbols)}] L/S verisi cekildi")
        time.sleep(0.1)

    # sinyalleri sembole gore grupla ve L/S esle
    by_symbol = {}
    for s in all_signals:
        by_symbol.setdefault(s.symbol, []).append(s)
    for symbol, sigs in by_symbol.items():
        attach_ls_ratio(sigs, ls_cache.get(symbol))

    with_ls = [s for s in all_signals if getattr(s, "long_account", None) is not None]
    log.info(f"{len(with_ls)}/{len(all_signals)} sinyalde L/S verisi eslendi (kalanlar veri yoklugu nedeniyle atlandi)")

    if not with_ls:
        log.error("Hicbir sinyalde L/S verisi eslenemedi - LS_MERGE_TOLERANCE_MIN'i artirmayi dene.")
        return

    # --- FADE icin dogru yeniden hesap: sembol+zaman uzerinden context'e geri git ---
    ctx_by_key = {(sym, s.signal_time_ms): (sym, df) for sym, df, s in signal_context}

    def fade_forward_move(sym, df, i, new_direction):
        entry_price = float(df["close"].iloc[i])
        future = df.iloc[i + 1: i + 1 + FORWARD_CANDLES]
        if new_direction == "LONG":
            max_fav = (future["high"].max() - entry_price) / entry_price * 100
        else:
            max_fav = (entry_price - future["low"].min()) / entry_price * 100
        hit = {t: bool(max_fav >= t) for t in TARGET_PCTS}
        return max_fav, hit

    # index eslemesi icin df icindeki open_time -> i haritasi sembol bazinda onceden kur
    df_by_symbol = {sym: df for sym, df, _ in signal_context}
    idx_map_by_symbol = {}
    for sym, df, _ in signal_context:
        if sym not in idx_map_by_symbol:
            idx_map_by_symbol[sym] = {int(ot): i for i, ot in enumerate(df["open_time"])}

    # ══════════════════════════════════════════════════════════════
    # RAPOR
    # ══════════════════════════════════════════════════════════════
    print("\n" + "=" * 95)
    print(f"STRATEJI B + L/S RATIO KARSILASTIRMA  (baz sinyal: {len(with_ls)}, L/S eslesen)")
    print("=" * 95)

    # BASELINE
    n, rates, avg_fav = eval_variant(with_ls, "baseline", 0)
    print(f"\n[BASELINE - L/S filtresi yok]  n={n}")
    for t in TARGET_PCTS:
        print(f"  %{t} hit-rate: {rates[t]:.1f}%")
    print(f"  Ort. lehte hareket: {avg_fav:.2f}%")

    # ONAY (confirmation)
    print(f"\n[ONAY - L/S sinyal yonuyle ayni tarafta]")
    header = f"{'Esik':>6} | {'n':>5} |" + "".join(f" %{t}|" for t in TARGET_PCTS) + " ort.fav%"
    print(header)
    for th in LS_THRESHOLDS:
        n, rates, avg_fav = eval_variant(with_ls, "confirm", th)
        row = f"{th*100:>5.0f}% | {n:>5} |"
        for t in TARGET_PCTS:
            row += f" {rates[t]:>4.1f}|"
        row += f" {avg_fav:>7.2f}%"
        print(row)

    # FADE - dogru hesap icin ozel dongu
    print(f"\n[FADE - L/S asiriysa yon tersine cevrilir (dogru forward-move ile yeniden hesaplanmis)]")
    print(header)
    for th in LS_THRESHOLDS:
        kept = []
        for s in with_ls:
            sym = s.symbol
            la = s.long_account
            df = df_by_symbol.get(sym)
            idx_map = idx_map_by_symbol.get(sym, {})
            i = idx_map.get(s.signal_time_ms)
            if df is None or i is None:
                continue

            if s.direction == "LONG" and la >= th:
                new_dir = "SHORT"
                max_fav, hit = fade_forward_move(sym, df, i, new_dir)
                kept.append((max_fav, hit))
            elif s.direction == "SHORT" and (1 - la) >= th:
                new_dir = "LONG"
                max_fav, hit = fade_forward_move(sym, df, i, new_dir)
                kept.append((max_fav, hit))
            else:
                kept.append((s.max_favorable_pct, s.hit_targets))

        n = len(kept)
        rates = {}
        for t in TARGET_PCTS:
            hits = sum(1 for _, ht in kept if ht.get(t))
            rates[t] = (hits / n * 100) if n else 0
        avg_fav = (sum(f for f, _ in kept) / n) if n else 0
        row = f"{th*100:>5.0f}% | {n:>5} |"
        for t in TARGET_PCTS:
            row += f" {rates[t]:>4.1f}|"
        row += f" {avg_fav:>7.2f}%"
        print(row)

    print("\nYorum: ONAY tablosunda 'n' esik yukseldikce azalir (daha az sinyal filtreden gecer);")
    print("hit-rate baseline'in USTUNE cikiyorsa L/S onayi gercekten degerli demektir.")
    print("FADE tablosunda n SABIT kalir (B sinyal sayisi ile ayni) cunku fade sadece yonu")
    print("degistirir, sinyali elemez; hit-rate baseline'dan yuksekse 'kalabaligin tersine")
    print("gitmek' bu strateji icin isliyor demektir.")

    # detay CSV
    rows = []
    for s in with_ls:
        rows.append({
            "symbol": s.symbol, "direction": s.direction, "signal_time": s.signal_time,
            "long_account": round(s.long_account, 4), "max_favorable_pct": round(s.max_favorable_pct, 2),
            "hit_5pct": s.hit_targets.get(5), "hit_10pct": s.hit_targets.get(10), "hit_20pct": s.hit_targets.get(20),
        })
    pd.DataFrame(rows).to_csv("strategy_b_ls_ratio_results.csv", index=False)
    log.info("Detayli sonuclar kaydedildi: strategy_b_ls_ratio_results.csv")


if __name__ == "__main__":
    main()
