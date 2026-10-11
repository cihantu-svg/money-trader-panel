import os
import re
import time
from datetime import datetime, timezone
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed

import numpy as np
import requests

# ==============================================================================
# AYARLAR (hepsinin varsayılanı hazır, dokunmana gerek yok)
# ==============================================================================
TIMEFRAME = os.getenv("TIMEFRAME", "1m")
SCAN_INTERVAL = int(os.getenv("SCAN_INTERVAL", 30))
MAX_WORKERS = int(os.getenv("MAX_WORKERS", 8))
MIN_QUOTE_VOL = float(os.getenv("MIN_QUOTE_VOL", 1_000_000))   # 24s hacim alt sınırı (USDT)

RANGE_BARS = int(os.getenv("RANGE_BARS", 20))        # kırılacak range: son 20 mum
VOL_MULT = float(os.getenv("VOL_MULT", 2.0))         # teyit mumu hacmi >= 2 x normal
IMBALANCE = float(os.getenv("IMBALANCE", 0.4))       # |delta| / hacim >= 0.4 (hacim tek taraftan akıyor)
MIN_RUN_PCT = float(os.getenv("MIN_RUN_PCT", 1.5))   # kırılım mumundan son teyide toplam hareket >= %1.5
COOLDOWN_MIN = int(os.getenv("COOLDOWN_MIN", 30))    # aynı coinde tekrar bildirim aralığı
LOOKBACK_BARS = 3                                    # son kaç mumda biten sinyal aransın

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

API_BASE = "https://fapi.binance.com"
session = requests.Session()
ban_until = 0.0
symbols_cache = {"ts": 0.0, "list": []}
last_alert = {}


# ==============================================================================
# BINANCE İSTEĞİ (ban / limit korumalı)
# ==============================================================================
def api_get(path, params=None, timeout=10):
    global ban_until
    if time.time() < ban_until:
        return None
    try:
        res = session.get(f"{API_BASE}{path}", params=params, timeout=timeout)
    except Exception:
        return None

    if res.status_code == 418:
        m = re.search(r"banned until (\d+)", res.text)
        ban_until = int(m.group(1)) / 1000.0 + 5 if m else time.time() + 300
    elif res.status_code == 429:
        ban_until = time.time() + int(res.headers.get("Retry-After", 30)) + 2
    else:
        used = int(res.headers.get("X-MBX-USED-WEIGHT-1M", 0) or 0)
        if used >= 1800:                      # limite yaklaşınca bir sonraki dakikaya kadar bekle
            ban_until = time.time() + (61 - time.time() % 60)
    return res


def get_symbols():
    if symbols_cache["list"] and time.time() - symbols_cache["ts"] < 3600:
        return symbols_cache["list"]

    info = api_get("/fapi/v1/exchangeInfo", timeout=20)
    tick = api_get("/fapi/v1/ticker/24hr", timeout=20)
    if info is None or tick is None or info.status_code != 200 or tick.status_code != 200:
        print("[Hata] Sembol listesi alınamadı (ban/limit olabilir).")
        return symbols_cache["list"]

    tradable = {
        s["symbol"] for s in info.json().get("symbols", [])
        if s.get("quoteAsset") == "USDT" and s.get("status") == "TRADING"
        and s.get("contractType") == "PERPETUAL"
    }
    vols = {t["symbol"]: float(t["quoteVolume"]) for t in tick.json()}
    symbols_cache["list"] = [s for s in tradable if vols.get(s, 0) >= MIN_QUOTE_VOL]
    symbols_cache["ts"] = time.time()
    return symbols_cache["list"]


# ==============================================================================
# ŞELALE SİNYALİ
# ==============================================================================
def find_signal(arr):
    """arr sütunları: 0 open_time, 1 open, 2 high, 3 low, 4 close, 5 volume, 9 taker_buy_base.
    Kırılım mumu (b) + 2 teyit mumu (b+1, b+2). Teyit mumları aynı yönde, güçlü ve tek taraflı olmalı."""
    n = len(arr)
    o, h, l, c, v = arr[:, 1], arr[:, 2], arr[:, 3], arr[:, 4], arr[:, 5]
    d = 2.0 * arr[:, 9] - v        # delta = taker alış - taker satış

    for off in range(LOOKBACK_BARS):
        e = n - 1 - off            # son teyit mumu
        b = e - 2                  # kırılım mumu
        if b - RANGE_BARS < 0:
            break
        rng_hi = h[b - RANGE_BARS:b].max()
        rng_lo = l[b - RANGE_BARS:b].min()
        vbase = v[b - RANGE_BARS:b].mean()
        if vbase <= 0 or o[b] <= 0:
            continue

        for direction, sign in (("LONG", 1), ("SHORT", -1)):
            broke = c[b] > rng_hi if sign == 1 else c[b] < rng_lo
            if not broke:
                continue

            ok, prev = True, c[b]
            for k in (b + 1, b + 2):
                moving = (c[k] - prev) * sign > 0          # fiyat aynı yönde ilerliyor
                flow = d[k] * sign > 0                     # delta aynı yönde
                heavy = v[k] >= VOL_MULT * vbase           # hacim yüksek
                onesided = abs(d[k]) >= IMBALANCE * v[k]   # hacmin çoğu tek taraftan
                if not (moving and flow and heavy and onesided):
                    ok = False
                    break
                prev = c[k]
            if not ok:
                continue

            run = (c[e] - o[b]) / o[b] * 100.0 * sign
            if run < MIN_RUN_PCT:
                continue

            return {
                "direction": direction,
                "level": rng_hi if sign == 1 else rng_lo,
                "price": c[e],
                "run": run,
                "vol_x": float(np.mean([v[b + 1], v[b + 2]]) / vbase),
                "imbalance": float(np.mean([abs(d[b + 1]) / v[b + 1], abs(d[b + 2]) / v[b + 2]])),
                "time": int(arr[e, 0]),
                "candles_ago": off,
            }
    return None


def analyze_symbol(symbol):
    try:
        res = api_get("/fapi/v1/klines", {"symbol": symbol, "interval": TIMEFRAME, "limit": 60})
        if res is None:
            return "rate_limit", "ban/limit aktif"
        if res.status_code != 200:
            return "api_error", f"HTTP {res.status_code}: {res.text[:100]}"
        rows = res.json()
        if not isinstance(rows, list) or len(rows) < RANGE_BARS + 6:
            return "short_data", None

        arr = np.array([[float(x) for x in r[:11]] for r in rows])
        arr = arr[arr[:, 6] < time.time() * 1000]      # kapanmamış mumu at
        sig = find_signal(arr)
        if sig is None:
            return "no_signal", None
        sig["symbol"] = symbol
        return "signal", sig
    except Exception as e:
        return "exception", f"{symbol}: {type(e).__name__}: {e}"


# ==============================================================================
# TELEGRAM + TARAMA DÖNGÜSÜ
# ==============================================================================
def send_telegram(msg):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("[Uyarı] Telegram ayarlı değil (TELEGRAM_TOKEN / TELEGRAM_CHAT_ID).")
        return
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": msg}, timeout=10,
        )
        if r.status_code != 200:
            print(f"[Hata] Telegram {r.status_code}: {r.text[:200]}")
    except Exception as e:
        print(f"[Hata] Telegram gönderilemedi: {e}")


def run_scanner():
    wait = ban_until - time.time()
    if wait > 0:
        print(f"[BAN/LIMIT] Binance istekleri duraklatıldı, {wait:.0f}s sonra devam.")
        time.sleep(min(wait, 300))
        return

    t0 = time.time()
    print(f"\n[{datetime.now(timezone.utc).strftime('%H:%M:%S')} UTC] 🔍 Tarama | {TIMEFRAME}")
    symbols = get_symbols()
    if not symbols:
        print("[!] Taranacak sembol yok.")
        return

    stats, errors, signals = Counter(), [], []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futs = {ex.submit(analyze_symbol, s): s for s in symbols}
        for f in as_completed(futs):
            status, data = f.result()
            stats[status] += 1
            if status == "signal":
                signals.append(data)
            elif status in ("api_error", "exception") and len(errors) < 3:
                errors.append(f"{futs[f]} -> {data}")

    elapsed = time.time() - t0
    print(f"[Tamamlandı] {len(symbols)} sembol, {elapsed:.0f}s | Sinyal: {stats['signal']} | {dict(stats)}")
    for e in errors:
        print(f"  - {e}")

    for s in signals:
        if s["time"] - last_alert.get(s["symbol"], 0) < COOLDOWN_MIN * 60_000:
            continue
        last_alert[s["symbol"]] = s["time"]

        icon = "🟢" if s["direction"] == "LONG" else "🔴"
        msg = (
            f"========================================\n"
            f"{icon} DELTA ŞELALE ({s['direction']})\n"
            f"Sembol: {s['symbol'].replace('USDT', '')}/USDT | {TIMEFRAME}\n"
            f"Kırılan seviye: {s['level']}\n"
            f"Fiyat: {s['price']}\n"
            f"Toplam hareket: %{s['run']:.2f}\n"
            f"Hacim: {s['vol_x']:.1f}x | Tek taraflılık: %{s['imbalance'] * 100:.0f}\n"
            f"========================================"
        )
        print(msg)
        send_telegram(msg)


def main():
    print("🚀 DELTA ŞELALE BOTU AKTİF")
    print(f"{TIMEFRAME} | Tarama {SCAN_INTERVAL}s | Range {RANGE_BARS} mum | Hacim {VOL_MULT}x | "
          f"Tek taraflılık %{IMBALANCE * 100:.0f} | Min hareket %{MIN_RUN_PCT} | Min 24s hacim {MIN_QUOTE_VOL:,.0f}")
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("[Uyarı] Telegram değişkenleri boş, sinyaller sadece konsola yazılacak.")
    while True:
        try:
            run_scanner()
        except Exception as e:
            print(f"[Sistem Hatası] {e}")
        time.sleep(SCAN_INTERVAL)


if __name__ == "__main__":
    main()
