import os, time, threading
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
import pandas as pd
import requests

TIMEFRAME     = os.environ.get("TIMEFRAME", "5m")
LOOK          = int(os.environ.get("LOOK", 12))
MIN_CVD_USDT  = float(os.environ.get("MIN_CVD_USDT", 5_000_000))  # 12 mumda net para (USDT)
MIN_RATIO     = float(os.environ.get("MIN_RATIO", 20))            # net delta / hacim, %
MIN_MOVE_PCT  = float(os.environ.get("MIN_MOVE_PCT", 1.0))        # fiyat hareketi, %
MIN_VOL_PCT   = float(os.environ.get("MIN_VOL_PCT", 0.30))        # ort. mum boyu, %
MIN_DAY_PCT   = float(os.environ.get("MIN_DAY_PCT", 5.0))         # net para / 24s hacim, %
COOLDOWN_MIN  = int(os.environ.get("COOLDOWN_MIN", 60))
MIN_VOLUME    = float(os.environ.get("MIN_VOLUME_USDT", 10_000_000))
SCAN_SEC      = int(os.environ.get("SCAN_INTERVAL_SEC", 180))
WORKERS       = int(os.environ.get("MAX_WORKERS", 8))

API   = "https://fapi.binance.com"
TOKEN = os.environ.get("TELEGRAM_TOKEN", "BURAYA_TOKEN")
CHAT  = os.environ.get("TELEGRAM_CHAT_ID", "BURAYA_CHAT_ID")

http = requests.Session()
son = {}
blocked_until = 0.0
lock = threading.Lock()


def tg(text):
    if TOKEN == "BURAYA_TOKEN":
        print(text, flush=True)
        return
    try:
        r = http.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                      data={"chat_id": CHAT, "text": text, "parse_mode": "HTML"}, timeout=10)
        if r.status_code != 200:
            print(f"Telegram HATA {r.status_code}: {r.text}", flush=True)
    except Exception as e:
        print(f"Telegram hatasi: {e}", flush=True)


def get(path, params=None):
    global blocked_until
    if time.time() < blocked_until:
        return None
    try:
        r = http.get(API + path, params=params, timeout=10)
    except Exception as e:
        print(f"İstek hatası: {e}", flush=True)
        return None
    if r.status_code in (418, 429):
        try:
            w = float(r.headers.get("Retry-After"))
        except (TypeError, ValueError):
            w = 300 if r.status_code == 418 else 60
        with lock:
            blocked_until = max(blocked_until, time.time() + w + 5)
        tg(f"⛔ Binance {r.status_code}, bot {int(w)} sn duruyor.")
        return None
    if r.status_code != 200:
        print(f"API {r.status_code}: {r.text[:150]}", flush=True)
        return None
    try:
        return r.json()
    except Exception:
        return None


def para(x):
    a = abs(x)
    return ("-" if x < 0 else "+") + (f"{a/1e6:.2f}M" if a >= 1e6 else f"{a/1e3:.0f}K")


def check(item):
    symbol, gun_hacim = item
    data = get("/fapi/v1/klines", {"symbol": symbol, "interval": TIMEFRAME, "limit": 55})
    if not isinstance(data, list) or len(data) < LOOK + 2:
        return None
    df = pd.DataFrame(data).iloc[:-1]            # canlı mumu at
    high = df[2].astype(float)
    low = df[3].astype(float)
    close = df[4].astype(float)
    qv = df[7].astype(float)
    buy = df[10].astype(float)
    delta = 2 * buy - qv

    net = delta.iloc[-LOOK:].sum()
    hacim = qv.iloc[-LOOK:].sum()
    son3 = delta.iloc[-3:].sum()
    hareket = (close.iloc[-1] / close.iloc[-LOOK - 1] - 1) * 100
    oran = net / hacim * 100 if hacim > 0 else 0
    vol_pct = ((high - low) / close).mean() * 100
    gun_pct = net / gun_hacim * 100 if gun_hacim > 0 else 0

    if vol_pct < MIN_VOL_PCT or abs(gun_pct) < MIN_DAY_PCT:
        return None

    if net >= MIN_CVD_USDT and oran >= MIN_RATIO and hareket >= MIN_MOVE_PCT and son3 > 0:
        yon = "AL"
    elif net <= -MIN_CVD_USDT and oran <= -MIN_RATIO and hareket <= -MIN_MOVE_PCT and son3 < 0:
        yon = "SAT"
    else:
        return None
    return dict(symbol=symbol, yon=yon, net=net, oran=oran, hareket=hareket,
                son3=son3, fiyat=close.iloc[-1], vol_pct=vol_pct, gun_pct=gun_pct)


def scan():
    print(f"\n[{datetime.now(timezone.utc):%H:%M:%S}] Tarama...", flush=True)
    t = get("/fapi/v1/ticker/24hr")
    if not isinstance(t, list):
        return
    items = [(d["symbol"], float(d["quoteVolume"])) for d in t
             if d["symbol"].endswith("USDT") and float(d.get("quoteVolume", 0)) >= MIN_VOLUME]
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        sonuclar = list(ex.map(check, items))
    n = 0
    for s in filter(None, sonuclar):
        key = (s["symbol"], s["yon"])
        if time.time() - son.get(key, 0) < COOLDOWN_MIN * 60:
            continue
        son[key] = time.time()
        n += 1
        ikon = "🟢" if s["yon"] == "AL" else "🔴"
        tg(f"{ikon} <b>PARA AKIŞI {s['yon']}</b>\n\n"
           f"• <b>Sembol</b>: {s['symbol']}\n"
           f"• <b>Net para ({LOOK} mum)</b>: {para(s['net'])} USDT (hacmin %{s['oran']:+.0f}'i)\n"
           f"• <b>Günlük hacme oranı</b>: %{s['gun_pct']:+.1f}\n"
           f"• <b>Son 3 mum</b>: {para(s['son3'])} USDT\n"
           f"• <b>Fiyat hareketi</b>: %{s['hareket']:+.1f}\n"
           f"• <b>Oynaklık (mum boyu)</b>: %{s['vol_pct']:.2f}\n"
           f"• <b>Fiyat</b>: {s['fiyat']}\n"
           f"• <b>TF</b>: {TIMEFRAME}")
    print(f"{len(items)} sembol | {n} sinyal", flush=True)


def main():
    tg("✅ Para akışı botu başladı.")
    while True:
        t0 = time.time()
        try:
            scan()
        except Exception as e:
            print(f"Genel hata: {e}", flush=True)
        time.sleep(max(SCAN_SEC - (time.time() - t0), 1))


if __name__ == "__main__":
    main()
