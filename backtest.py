import os
import time
import pickle
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed

import numpy as np
import pandas as pd
import requests

# ==============================================================================
# YAPILANDIRMA
# ==============================================================================
API_BASE = "https://fapi.binance.com"

TIMEFRAMES = [t.strip() for t in os.getenv("TIMEFRAMES", "1m,5m,15m").split(",")]
TF_MINUTES = {"1m": 1, "5m": 5, "15m": 15}
# Her zaman diliminde kaç günlük geçmiş taransın (1m çok veri çektiği için kısa)
DAYS_MAP = {
    "1m": int(os.getenv("DAYS_1M", 3)),
    "5m": int(os.getenv("DAYS_5M", 14)),
    "15m": int(os.getenv("DAYS_15M", 30)),
}

# Strateji (canlı botla aynı)
MIN_CANDLE_PCT = float(os.getenv("MIN_CANDLE_PCT", 2.0))
VOLUME_MULTIPLIER = float(os.getenv("VOLUME_MULTIPLIER", 3.0))
AVG_WINDOW = 23  # canlı kodla aynı: hareket mumundan önceki 23 mum

# Pozisyon yönetimi
SL_PCT = float(os.getenv("SL_PCT", 3.0))
TP_PCT = float(os.getenv("TP_PCT", 6.0))
MAX_HOLD_HOURS = float(os.getenv("MAX_HOLD_HOURS", 24))  # süre dolunca piyasadan kapat
FEE_PCT = float(os.getenv("FEE_PCT", 0.04))              # taker komisyonu, işlem başına (giriş+çıkış = 2x)

# Evren
TOP_N = int(os.getenv("TOP_N", 100))                     # 24s hacme göre ilk N sembol
MIN_QUOTE_VOL = float(os.getenv("MIN_QUOTE_VOL", 3_000_000))  # 24s USDT hacim alt sınırı
MAX_WORKERS = int(os.getenv("MAX_WORKERS", 5))

# Cache ve çıktı
USE_CACHE = os.getenv("USE_CACHE", "true").lower() == "true"
CACHE_DIR = "bt_cache"
OUT_DIR = "bt_results"
os.makedirs(CACHE_DIR, exist_ok=True)
os.makedirs(OUT_DIR, exist_ok=True)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

session = requests.Session()


# ==============================================================================
# VERİ ÇEKME
# ==============================================================================
def get_json(url, params=None, retries=5):
    for attempt in range(1, retries + 1):
        try:
            r = session.get(url, params=params, timeout=15)
            if r.status_code == 200:
                return r.json()
            if r.status_code in (418, 429):
                wait = int(r.headers.get("Retry-After", 5 * attempt))
                print(f"[Rate limit] {r.status_code} -> {wait}s bekleniyor")
                time.sleep(wait)
                continue
            if r.status_code == 400:  # geçersiz sembol/parametre, tekrar denemeye değmez
                return None
            time.sleep(1.5 * attempt)
        except Exception:
            time.sleep(1.5 * attempt)
    return None


def get_symbols():
    info = get_json(f"{API_BASE}/fapi/v1/exchangeInfo")
    tick = get_json(f"{API_BASE}/fapi/v1/ticker/24hr")
    if not info or not tick:
        print("[Hata] Sembol listesi alınamadı (IP engeli / bağlantı?)")
        return []
    ok = {
        s["symbol"]
        for s in info.get("symbols", [])
        if s.get("quoteAsset") == "USDT"
        and s.get("status") == "TRADING"
        and s.get("contractType") == "PERPETUAL"
    }
    rows = [(t["symbol"], float(t["quoteVolume"])) for t in tick if t["symbol"] in ok]
    rows = [r for r in rows if r[1] >= MIN_QUOTE_VOL]
    rows.sort(key=lambda x: x[1], reverse=True)
    return [r[0] for r in rows[:TOP_N]]


def fetch_klines(symbol, tf, days):
    cache_file = os.path.join(CACHE_DIR, f"{symbol}_{tf}_{days}d.pkl")
    if USE_CACHE and os.path.exists(cache_file):
        if time.time() - os.path.getmtime(cache_file) < 6 * 3600:
            with open(cache_file, "rb") as f:
                return pickle.load(f)

    now_ms = int(time.time() * 1000)
    start = now_ms - days * 86_400_000
    rows = []
    while start < now_ms:
        data = get_json(
            f"{API_BASE}/fapi/v1/klines",
            {"symbol": symbol, "interval": tf, "startTime": start, "limit": 1500},
        )
        if not data:
            break
        rows.extend(data)
        if len(data) < 1500:
            break
        start = data[-1][0] + 1

    if not rows:
        return None

    df = pd.DataFrame(rows).iloc[:, :11].astype(float)
    df.columns = [
        "open_time", "open", "high", "low", "close", "volume",
        "close_time", "quote_volume", "trades", "taker_buy_base", "taker_buy_quote",
    ]
    df = df.drop_duplicates("open_time").sort_values("open_time").reset_index(drop=True)
    df = df[df["close_time"] < now_ms].reset_index(drop=True)  # kapanmamış mumu at

    with open(cache_file, "wb") as f:
        pickle.dump(df, f)
    return df


# ==============================================================================
# SİNYAL + İŞLEM SİMÜLASYONU
# ==============================================================================
def backtest_symbol(symbol, df, tf):
    n = len(df)
    if n < AVG_WINDOW + 5:
        return []

    o = df["open"].to_numpy()
    h = df["high"].to_numpy()
    l = df["low"].to_numpy()
    c = df["close"].to_numpy()
    v = df["volume"].to_numpy()
    tb = df["taker_buy_base"].to_numpy()
    t_open = df["open_time"].to_numpy()

    delta = 2.0 * tb - v  # taker_buy - taker_sell
    body = np.abs(c - o) / o * 100.0
    avg = pd.Series(v).rolling(AVG_WINDOW).mean().shift(1).to_numpy()  # m1'den önceki 23 mum

    # j = 1. mum (hareket), j+1 = 2. mum (teyit)
    with np.errstate(invalid="ignore", divide="ignore"):
        spike = (avg[:-1] > 0) & (v[:-1] >= avg[:-1] * VOLUME_MULTIPLIER) & (body[:-1] >= MIN_CANDLE_PCT)
        long_sig = spike & (c[:-1] > o[:-1]) & (delta[:-1] > 0) & (c[1:] > c[:-1]) & (delta[1:] > 0)
        short_sig = spike & (c[:-1] < o[:-1]) & (delta[:-1] < 0) & (c[1:] < c[:-1]) & (delta[1:] < 0)

    sig_idx = np.where(long_sig | short_sig)[0]
    max_hold = max(1, int(MAX_HOLD_HOURS * 60 / TF_MINUTES[tf]))
    trades = []
    busy_until = -1

    for j in sig_idx:
        k = j + 1          # teyit mumu (sinyalin oluştuğu mum)
        e = k + 1          # giriş: bir sonraki mumun açılışı (look-ahead yok)
        if e >= n or k <= busy_until:
            continue

        direction = "LONG" if long_sig[j] else "SHORT"
        entry = o[e]
        if direction == "LONG":
            sl, tp = entry * (1 - SL_PCT / 100), entry * (1 + TP_PCT / 100)
        else:
            sl, tp = entry * (1 + SL_PCT / 100), entry * (1 - TP_PCT / 100)

        last = min(n - 1, e + max_hold - 1)
        reason, exit_i, exit_price = None, last, c[last]

        for t in range(e, last + 1):
            if direction == "LONG":
                sl_hit, tp_hit = l[t] <= sl, h[t] >= tp
            else:
                sl_hit, tp_hit = h[t] >= sl, l[t] <= tp
            # Aynı mumda ikisi de görülürse en kötüsünü say (muhafazakâr)
            if sl_hit:
                reason, exit_i, exit_price = "SL", t, sl
                break
            if tp_hit:
                reason, exit_i, exit_price = "TP", t, tp
                break

        if reason is None:
            if e + max_hold - 1 > n - 1:
                reason = "OPEN"      # veri bitti, sonuçlanmadı -> istatistiğe girmez
            else:
                reason = "TIMEOUT"

        move = (exit_price / entry - 1) * 100.0
        pnl = (move if direction == "LONG" else -move) - 2 * FEE_PCT

        trades.append({
            "timeframe": tf,
            "symbol": symbol,
            "direction": direction,
            "signal_time": datetime.fromtimestamp(t_open[k] / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M"),
            "entry": entry,
            "exit": exit_price,
            "reason": reason,
            "bars_held": int(exit_i - e + 1),
            "pnl_pct": round(pnl, 3),
            "m1_body_pct": round(body[j], 2),
            "vol_multi": round(v[j] / avg[j], 1),
        })
        busy_until = exit_i

    return trades


def process(symbol, tf, days):
    df = fetch_klines(symbol, tf, days)
    if df is None or df.empty:
        return symbol, None
    return symbol, backtest_symbol(symbol, df, tf)


# ==============================================================================
# İSTATİSTİK
# ==============================================================================
def summarize(tdf, tf):
    tdf = tdf[tdf["reason"] != "OPEN"]
    n = len(tdf)
    print(f"\n{'=' * 60}\n⏱  ZAMAN DİLİMİ: {tf}  |  {DAYS_MAP[tf]} gün\n{'=' * 60}")
    if n == 0:
        print("Hiç sinyal/işlem yok. (Filtreler bu zaman diliminde çok sıkı olabilir.)")
        return None

    wins = tdf[tdf["reason"] == "TP"]
    losses = tdf[tdf["reason"] == "SL"]
    timeouts = tdf[tdf["reason"] == "TIMEOUT"]
    pnl = tdf["pnl_pct"]

    gross_win = pnl[pnl > 0].sum()
    gross_loss = abs(pnl[pnl < 0].sum())
    pf = gross_win / gross_loss if gross_loss > 0 else float("inf")

    ordered = tdf.sort_values("signal_time")
    equity = ordered["pnl_pct"].cumsum()
    max_dd = (equity.cummax() - equity).max()

    be_wr = (SL_PCT + 2 * FEE_PCT) / (SL_PCT + TP_PCT) * 100  # kabaca başabaş isabet oranı

    print(f"Toplam işlem        : {n}")
    print(f"TP / SL / Timeout   : {len(wins)} / {len(losses)} / {len(timeouts)}")
    print(f"TP oranı            : %{len(wins) / n * 100:.1f}   (başabaş ≈ %{be_wr:.1f})")
    print(f"Ort. işlem getirisi : %{pnl.mean():.3f}")
    print(f"Toplam getiri (sabit lot, basit toplam): %{pnl.sum():.1f}")
    print(f"Profit factor       : {pf:.2f}")
    print(f"Maks. düşüş (toplam üzerinden): %{max_dd:.1f}")
    for d in ("LONG", "SHORT"):
        sub = tdf[tdf["direction"] == d]
        if len(sub):
            w = (sub["reason"] == "TP").mean() * 100
            print(f"  {d:<5}: {len(sub):>4} işlem | TP %{w:.1f} | ort. %{sub['pnl_pct'].mean():.3f}")

    return {
        "tf": tf, "trades": n, "tp": len(wins), "sl": len(losses), "timeout": len(timeouts),
        "tp_rate": round(len(wins) / n * 100, 1), "avg_pnl": round(pnl.mean(), 3),
        "total_pnl": round(pnl.sum(), 1), "pf": round(pf, 2), "max_dd": round(max_dd, 1),
    }


def send_telegram_file(path, caption=""):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        with open(path, "rb") as f:
            r = requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendDocument",
                data={"chat_id": TELEGRAM_CHAT_ID, "caption": caption},
                files={"document": f},
                timeout=60,
            )
        if r.status_code != 200:
            print(f"[Telegram] {r.status_code}: {r.text[:200]}")
    except Exception as e:
        print(f"[Telegram] gönderilemedi: {e}")


# ==============================================================================
# ANA AKIŞ
# ==============================================================================
def main():
    print("📊 SAF DELTA BREAKOUT BACKTEST")
    print(f"SL: %{SL_PCT} | TP: %{TP_PCT} | Komisyon: %{FEE_PCT}/işlem | Maks. bekleme: {MAX_HOLD_HOURS}s")
    print(f"Filtre: gövde >= %{MIN_CANDLE_PCT} | hacim >= {VOLUME_MULTIPLIER}x | Evren: ilk {TOP_N} sembol")

    symbols = get_symbols()
    if not symbols:
        return
    print(f"Test edilecek sembol sayısı: {len(symbols)}")

    summaries, all_trades = [], []

    for tf in TIMEFRAMES:
        if tf not in TF_MINUTES:
            print(f"[Atlandı] Desteklenmeyen zaman dilimi: {tf}")
            continue
        days = DAYS_MAP[tf]
        print(f"\n[{tf}] Veri çekiliyor ve test ediliyor ({days} gün)...")
        tf_trades, failed, done = [], 0, 0

        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
            futs = {ex.submit(process, s, tf, days): s for s in symbols}
            for f in as_completed(futs):
                sym, res = f.result()
                done += 1
                if res is None:
                    failed += 1
                else:
                    tf_trades.extend(res)
                if done % 20 == 0:
                    print(f"  {done}/{len(symbols)} sembol işlendi...")

        if failed:
            print(f"[Uyarı] {failed} sembolde veri alınamadı.")

        if tf_trades:
            tdf = pd.DataFrame(tf_trades)
            path = os.path.join(OUT_DIR, f"backtest_{tf}.csv")
            tdf.to_csv(path, index=False)
            all_trades.append(tdf)
            s = summarize(tdf, tf)
            if s:
                summaries.append(s)
            send_telegram_file(path, f"Backtest {tf} | SL %{SL_PCT} TP %{TP_PCT}")
        else:
            summarize(pd.DataFrame(columns=["reason"]), tf)

    if summaries:
        print(f"\n{'=' * 60}\n📋 KARŞILAŞTIRMA\n{'=' * 60}")
        print(pd.DataFrame(summaries).to_string(index=False))
        pd.DataFrame(summaries).to_csv(os.path.join(OUT_DIR, "ozet.csv"), index=False)
    if all_trades:
        pd.concat(all_trades).to_csv(os.path.join(OUT_DIR, "tum_islemler.csv"), index=False)
        print(f"\nCSV dosyaları '{OUT_DIR}/' klasöründe.")


if __name__ == "__main__":
    main()
