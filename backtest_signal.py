# -*- coding: utf-8 -*-
"""
BACKTEST: 15dk %7+ mum sinyali - MOMENTUM vs MEAN-REVERSION karsilastirma

SINYAL (giris kosulu):
  - 15 dakikalik kapanmis mumun govdesi (close-open)/open >= %7 (YUKARI olay)
    veya <= -%7 (ASAGI olay)
  - O anki trailing 24 saatlik hacim (son 96x15dk mumun toplam quote volume'u)
    >= 3,000,000 USDT

IKI STRATEJI, AYNI SINYAL SETI UZERINDE, BAGIMSIZ SIMULE EDILIR:
  MOMENTUM      : mum yukari kapandiysa LONG, asagi kapandiysa SHORT
  MEAN-REVERSION: mum yukari kapandiysa SHORT, asagi kapandiysa LONG

POZISYON YONETIMI:
  - Giris fiyati = sinyal mumunun kapanis fiyati
  - Stop-Loss  : %5 (giris fiyatindan)
  - Take-Profit: %10 (giris fiyatindan)
  - Bir coin icin, o STRATEJIDE acik pozisyon varken yeni sinyal ATLANIR
    (gercek bir bot da ayni coinde ust uste pozisyon acmaz)
  - Bir sonraki mumlarda intrabar (mum ici) high/low ile TP/SL kontrol edilir.
    Ayni mum icinde hem TP hem SL'e deginilmisse, KOTUMSER varsayimla SL
    once tetiklenmis kabul edilir (gercekte hangisinin once oldugu bilinmez,
    bu backtest'i iyimser sonuc vermekten korur).
  - Islem fon suresi icinde (test penceresi sonuna kadar) TP/SL'e ulasmazsa
    "ACIK KALDI" olarak isaretlenir, kapanis fiyatindan gerceklesmemis PNL
    hesaplanir ama kazanma oranina dahil edilmez.

UCRET: Binance Futures taker ucreti varsayilan %0.04/islem (%0.08 round-trip)
PNL'den dusulur. 0 yapmak icin TAKER_FEE_PCT_PER_SIDE = 0.0 yap.

ONEMLI: Bu backtest gecmis veriye dayanir, gelecekteki performansi garanti
etmez. Sadece bilgi/arastirma amaclidir, yatirim tavsiyesi degildir.
"""
import os
import time
import logging
import csv
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed

import numpy as np
import pandas as pd
import requests

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════════════
# AYARLAR
# ══════════════════════════════════════════════════════════════════
DAYS_BACK = int(os.getenv("DAYS_BACK", "20"))              # backtest penceresi (gun)
MAX_COINS = int(os.getenv("MAX_COINS", "300"))              # 24h hacme gore en yuksek N coin
EVENT_TF = "15m"

BODY_PCT_THRESHOLD = float(os.getenv("BODY_PCT_THRESHOLD", "7.0"))
MIN_QUOTE_VOLUME_24H = float(os.getenv("MIN_QUOTE_VOLUME_24H", "3000000"))

STOP_LOSS_PCT = float(os.getenv("STOP_LOSS_PCT", "5.0"))
TAKE_PROFIT_PCT = float(os.getenv("TAKE_PROFIT_PCT", "10.0"))
TAKER_FEE_PCT_PER_SIDE = float(os.getenv("TAKER_FEE_PCT_PER_SIDE", "0.04"))  # %0 yapmak icin degistir

MAX_WORKERS = int(os.getenv("MAX_WORKERS", "8"))
REQUEST_TIMEOUT = 15
MAX_RETRIES = 3
RETRY_BACKOFF_BASE = 0.5
KLINES_LIMIT = 1500  # Binance tek istekte max

BINANCE_FAPI = "https://fapi.binance.com"
OUTPUT_DIR = os.getenv("OUTPUT_DIR", ".")

session = requests.Session()
_adapter = requests.adapters.HTTPAdapter(pool_connections=MAX_WORKERS + 5, pool_maxsize=MAX_WORKERS + 10)
session.mount("https://", _adapter)


# ══════════════════════════════════════════════════════════════════
# BINANCE VERI CEKME (retry'li)
# ══════════════════════════════════════════════════════════════════
def _get_with_retry(url, params=None, timeout=REQUEST_TIMEOUT):
    last_exc = None
    for attempt in range(MAX_RETRIES + 1):
        try:
            r = session.get(url, params=params, timeout=timeout)
            if r.status_code == 200:
                return r.json()
            wait = RETRY_BACKOFF_BASE * (2 ** attempt)
            log.warning(f"HTTP {r.status_code} ({url}), {wait:.1f}sn sonra tekrar")
            time.sleep(wait)
            last_exc = Exception(f"HTTP {r.status_code}")
        except requests.exceptions.RequestException as e:
            last_exc = e
            time.sleep(RETRY_BACKOFF_BASE * (2 ** attempt))
    raise last_exc if last_exc else Exception("Bilinmeyen istek hatasi")


def get_top_symbols(n):
    """24h hacme gore en yuksek N USDT-M perpetual sembolu doner."""
    info = _get_with_retry(f"{BINANCE_FAPI}/fapi/v1/exchangeInfo", timeout=10)
    perpetual_usdt = {
        s["symbol"] for s in info["symbols"]
        if s["symbol"].endswith("USDT") and s.get("contractType") == "PERPETUAL"
        and s.get("status") == "TRADING"
    }
    tickers = _get_with_retry(f"{BINANCE_FAPI}/fapi/v1/ticker/24hr", timeout=15)
    rows = [(t["symbol"], float(t.get("quoteVolume", 0))) for t in tickers if t["symbol"] in perpetual_usdt]
    rows.sort(key=lambda x: x[1], reverse=True)
    return [sym for sym, _ in rows[:n]]


def fetch_klines_range(symbol, interval, start_ms, end_ms):
    """start_ms - end_ms arasindaki tum mumlari sayfalayarak ceker."""
    all_rows = []
    cursor = start_ms
    while cursor < end_ms:
        try:
            batch = _get_with_retry(
                f"{BINANCE_FAPI}/fapi/v1/klines",
                params={"symbol": symbol, "interval": interval, "startTime": cursor,
                        "endTime": end_ms, "limit": KLINES_LIMIT},
                timeout=REQUEST_TIMEOUT,
            )
        except Exception as e:
            log.debug(f"{symbol} klines hata: {e}")
            break
        if not batch:
            break
        all_rows.extend(batch)
        last_open_time = batch[-1][0]
        if len(batch) < KLINES_LIMIT:
            break
        cursor = last_open_time + 1
        time.sleep(0.05)

    if not all_rows:
        return None

    df = pd.DataFrame(all_rows, columns=[
        "open_time", "open", "high", "low", "close", "volume", "close_time",
        "quote_volume", "trades", "taker_buy_base", "taker_buy_quote", "ignore"])
    for c in ["open", "high", "low", "close", "volume", "quote_volume"]:
        df[c] = df[c].astype(float)
    df["open_time"] = df["open_time"].astype(np.int64)
    df["close_time"] = df["close_time"].astype(np.int64)
    return df.drop_duplicates(subset="open_time").sort_values("open_time").reset_index(drop=True)


# ══════════════════════════════════════════════════════════════════
# TEK COIN ICIN: SINYALLERI BUL + IKI STRATEJIYI SIMULE ET
# ══════════════════════════════════════════════════════════════════
def simulate_symbol(symbol, test_start_ms, fetch_start_ms, now_ms):
    df = fetch_klines_range(symbol, EVENT_TF, fetch_start_ms, now_ms)
    if df is None or len(df) < 100:
        return []

    df["body_pct"] = (df["close"] - df["open"]) / df["open"] * 100
    df["rolling_24h_vol"] = df["quote_volume"].rolling(96, min_periods=96).sum()

    trades = []
    # her strateji icin ayri "acik pozisyon" takibi (ayni coinde ust uste islem yok)
    open_trade = {"MOMENTUM": None, "REVERSION": None}

    n = len(df)
    for i in range(n):
        row = df.iloc[i]

        # --- once acik pozisyonlari kontrol et (TP/SL tetiklendi mi) ---
        for strat in ("MOMENTUM", "REVERSION"):
            ot = open_trade[strat]
            if ot is None:
                continue
            high, low = float(row["high"]), float(row["low"])
            if ot["direction"] == "LONG":
                tp_price = ot["entry_price"] * (1 + TAKE_PROFIT_PCT / 100)
                sl_price = ot["entry_price"] * (1 - STOP_LOSS_PCT / 100)
                hit_tp, hit_sl = high >= tp_price, low <= sl_price
            else:  # SHORT
                tp_price = ot["entry_price"] * (1 - TAKE_PROFIT_PCT / 100)
                sl_price = ot["entry_price"] * (1 + STOP_LOSS_PCT / 100)
                hit_tp, hit_sl = low <= tp_price, high >= sl_price

            if hit_tp and hit_sl:
                result = "SL"   # kotumser varsayim: ayni mumda ikisi de degdiyse SL once
            elif hit_sl:
                result = "SL"
            elif hit_tp:
                result = "TP"
            else:
                continue  # bu mumda kapanmadi, acik kalmaya devam

            raw_pnl = TAKE_PROFIT_PCT if result == "TP" else -STOP_LOSS_PCT
            pnl_pct = raw_pnl - (2 * TAKER_FEE_PCT_PER_SIDE)  # giris+cikis ucreti
            trades.append({
                "symbol": symbol, "strategy": strat, "direction": ot["direction"],
                "entry_time": ot["entry_time"], "entry_price": ot["entry_price"],
                "exit_time": int(row["close_time"]), "exit_price": float(row["close"]),
                "result": result, "pnl_pct": pnl_pct,
                "event_body_pct": ot["event_body_pct"],
            })
            open_trade[strat] = None

        # --- test penceresinden onceyse (sadece warmup icin var), yeni sinyal aramaya gecme ---
        if row["open_time"] < test_start_ms:
            continue

        vol24 = row["rolling_24h_vol"]
        if pd.isna(vol24) or vol24 < MIN_QUOTE_VOLUME_24H:
            continue

        body_pct = row["body_pct"]
        if abs(body_pct) < BODY_PCT_THRESHOLD:
            continue

        entry_price = float(row["close"])
        entry_time = int(row["close_time"])

        # MOMENTUM: mum yonunde
        if open_trade["MOMENTUM"] is None:
            direction = "LONG" if body_pct > 0 else "SHORT"
            open_trade["MOMENTUM"] = {
                "direction": direction, "entry_price": entry_price,
                "entry_time": entry_time, "event_body_pct": body_pct,
            }

        # REVERSION: mumun tersi
        if open_trade["REVERSION"] is None:
            direction = "SHORT" if body_pct > 0 else "LONG"
            open_trade["REVERSION"] = {
                "direction": direction, "entry_price": entry_price,
                "entry_time": entry_time, "event_body_pct": body_pct,
            }

    # test suresi bitince hala acik olanlar -> gerceklesmemis (OPEN) olarak kaydet
    last_close = float(df.iloc[-1]["close"])
    last_time = int(df.iloc[-1]["close_time"])
    for strat in ("MOMENTUM", "REVERSION"):
        ot = open_trade[strat]
        if ot is None:
            continue
        if ot["direction"] == "LONG":
            raw_pnl = (last_close - ot["entry_price"]) / ot["entry_price"] * 100
        else:
            raw_pnl = (ot["entry_price"] - last_close) / ot["entry_price"] * 100
        pnl_pct = raw_pnl - (2 * TAKER_FEE_PCT_PER_SIDE)
        trades.append({
            "symbol": symbol, "strategy": strat, "direction": ot["direction"],
            "entry_time": ot["entry_time"], "entry_price": ot["entry_price"],
            "exit_time": last_time, "exit_price": last_close,
            "result": "OPEN", "pnl_pct": pnl_pct, "event_body_pct": ot["event_body_pct"],
        })

    return trades


# ══════════════════════════════════════════════════════════════════
# OZET ISTATISTIK
# ══════════════════════════════════════════════════════════════════
def summarize(trades, strategy_name):
    rows = [t for t in trades if t["strategy"] == strategy_name]
    closed = [t for t in rows if t["result"] in ("TP", "SL")]
    open_ = [t for t in rows if t["result"] == "OPEN"]

    wins = [t for t in closed if t["result"] == "TP"]
    losses = [t for t in closed if t["result"] == "SL"]

    n_closed = len(closed)
    win_rate = (len(wins) / n_closed * 100) if n_closed else 0.0
    total_pnl = sum(t["pnl_pct"] for t in closed)
    avg_pnl = (total_pnl / n_closed) if n_closed else 0.0

    long_trades = [t for t in rows if t["direction"] == "LONG"]
    short_trades = [t for t in rows if t["direction"] == "SHORT"]

    print(f"\n{'=' * 60}")
    print(f"STRATEJI: {strategy_name}")
    print(f"{'=' * 60}")
    print(f"Toplam sinyal (islem)      : {len(rows)}  (LONG: {len(long_trades)} | SHORT: {len(short_trades)})")
    print(f"Kapanan islem              : {n_closed}   (hala acik: {len(open_)})")
    print(f"Kazanan (TP)               : {len(wins)}")
    print(f"Kaybeden (SL)              : {len(losses)}")
    print(f"Kazanma orani              : %{win_rate:.2f}")
    print(f"Toplam getiri (basit toplam, compounding YOK) : %{total_pnl:.2f}")
    print(f"Islem basi ortalama getiri : %{avg_pnl:.2f}")
    if n_closed:
        avg_days = DAYS_BACK
        print(f"Gunluk ortalama sinyal     : {len(rows) / avg_days:.2f}")
    return {
        "strategy": strategy_name, "total_trades": len(rows), "closed": n_closed,
        "open": len(open_), "wins": len(wins), "losses": len(losses),
        "win_rate": win_rate, "total_pnl_pct": total_pnl, "avg_pnl_pct": avg_pnl,
    }


def save_csv(trades, path):
    if not trades:
        return
    fieldnames = ["symbol", "strategy", "direction", "entry_time", "entry_price",
                  "exit_time", "exit_price", "result", "pnl_pct", "event_body_pct"]
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for t in trades:
            row = dict(t)
            row["entry_time"] = datetime.fromtimestamp(row["entry_time"] / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
            row["exit_time"] = datetime.fromtimestamp(row["exit_time"] / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
            writer.writerow(row)
    log.info(f"Tum islemler CSV'ye yazildi: {path}")


# ══════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════
def main():
    now_ms = int(time.time() * 1000)
    test_start_ms = now_ms - DAYS_BACK * 24 * 60 * 60 * 1000
    fetch_start_ms = test_start_ms - 1 * 24 * 60 * 60 * 1000  # 1 gunluk warmup (rolling 24h hacim icin)

    log.info(f"Backtest penceresi: son {DAYS_BACK} gun")
    log.info(f"Kriterler: govde >= %{BODY_PCT_THRESHOLD} | 24h hacim >= ${MIN_QUOTE_VOLUME_24H:,.0f} | "
              f"SL %{STOP_LOSS_PCT} | TP %{TAKE_PROFIT_PCT} | ucret %{TAKER_FEE_PCT_PER_SIDE}/islem")

    log.info(f"En yuksek {MAX_COINS} coin (24h hacme gore) cekiliyor...")
    symbols = get_top_symbols(MAX_COINS)
    log.info(f"{len(symbols)} coin bulundu, mum verisi cekiliyor ve simule ediliyor "
              f"({MAX_WORKERS} paralel worker)...")

    all_trades = []
    done = 0
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {
            executor.submit(simulate_symbol, s, test_start_ms, fetch_start_ms, now_ms): s
            for s in symbols
        }
        for future in as_completed(futures):
            sym = futures[future]
            try:
                trades = future.result()
                all_trades.extend(trades)
            except Exception as e:
                log.warning(f"{sym} simulasyon hatasi: {e}")
            done += 1
            if done % 25 == 0:
                log.info(f"{done}/{len(symbols)} coin tamamlandi...")

    log.info(f"Tum coinler tamamlandi. Toplam kayitli islem: {len(all_trades)}")

    momentum_summary = summarize(all_trades, "MOMENTUM")
    reversion_summary = summarize(all_trades, "REVERSION")

    csv_path = os.path.join(OUTPUT_DIR, f"backtest_trades_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv")
    save_csv(all_trades, csv_path)

    print(f"\n{'=' * 60}")
    print("SONUC")
    print(f"{'=' * 60}")
    print(f"MOMENTUM       -> Kazanma: %{momentum_summary['win_rate']:.2f} | "
          f"Toplam getiri: %{momentum_summary['total_pnl_pct']:.2f} | "
          f"Islem: {momentum_summary['total_trades']}")
    print(f"MEAN-REVERSION -> Kazanma: %{reversion_summary['win_rate']:.2f} | "
          f"Toplam getiri: %{reversion_summary['total_pnl_pct']:.2f} | "
          f"Islem: {reversion_summary['total_trades']}")
    print("\nNot: Bu sonuclar gecmis veriye dayanir, gelecek performansi garanti etmez.")
    print(f"Detayli islem listesi: {csv_path}")


if __name__ == "__main__":
    main()
