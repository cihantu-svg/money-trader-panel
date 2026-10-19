import os, time
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
import pandas as pd
import requests

TIMEFRAME    = os.environ.get("TIMEFRAME", "5m")
LOOK         = int(os.environ.get("LOOK", 6))                # 6 mum = 30 dk
KAT          = float(os.environ.get("KAT", 3.0))             # net para, normalin kaç katı
MIN_OI_PCT   = float(os.environ.get("MIN_OI_PCT", 0.5))      # OI artışı, %
MIN_MOVE_PCT = float(os.environ.get("MIN_MOVE_PCT", 0.3))    # fiyat hareketi, %
COOLDOWN_MIN = int(os.environ.get("COOLDOWN_MIN", 60))
MIN_VOLUME   = float(os.environ.get("MIN_VOLUME_USDT", 10_000_000))
SCAN_SEC     = int(os.environ.get("SCAN_INTERVAL_SEC", 180))
WORKERS      = int(os.environ.get("MAX_WORKERS", 6))

API   = "https://fapi.binance.com"
TOKEN = os.environ.get("TELEGRAM_TOKEN", "BURAYA_TOKEN")
CHAT  = os.environ.get("TELEGRAM_CHAT_ID", "BURAYA_CHAT_ID")

http = requests.Session()
son = {}
blocked_until = 0.0


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


def check(symbol):
    data = get("/fapi/v1/klines", {"symbol": symbol, "interval": TIMEFRAME, "limit": 120})
    if not isinstance(data, list) or len(data) < LOOK * 4:
        return None
    df = pd.DataFrame(data).iloc[:-1]                 # canlı mumu at
    close = df[4].astype(float)
    delta = 2 * df[10].astype(float) - df[7].astype(float)   # USDT net delta

    toplam = delta.rolling(LOOK).sum()                # her an için LOOK mumluk net para
    net = toplam.iloc[-1]
    normal = toplam.iloc[:-LOOK].abs().mean()         # coinin kendi normal akışı
    if not normal or normal <= 0:
        return None
    kat = abs(net) / normal
    hareket = (close.iloc[-1] / close.iloc[-LOOK - 1] - 1) * 100

    if net > 0 and hareket >= MIN_MOVE_PCT:
        yon = "AL"
    elif net < 0 and hareket <= -MIN_MOVE_PCT:
        yon = "SAT"
    else:
        return None
    if kat < KAT:
        return None

    # Son filtre: yeni para giriyor mu (OI). Sadece adaylar için sorulur.
    oi = get("/futures/data/openInterestHist",
             {"symbol": symbol, "period": TIMEFRAME, "limit": LOOK + 1})
    if not isinstance(oi, list) or len(oi) < LOOK + 1:
        return None
    oi_ilk = float(oi[0]["sumOpenInterestValue"])
    oi_son = float(oi[-1]["sumOpenInterestValue"])
    if oi_ilk <= 0:
        return None
    oi_pct = (oi_son / oi_ilk - 1) * 100
    if oi_pct < MIN_OI_PCT:
        return None

    return dict(symbol=symbol, yon=yon, net=net, kat=kat, hareket=hareket,
                oi=oi_pct, fiyat=close.iloc[-1])


def scan():
    print(f"\n[{datetime.now(timezone.utc):%H:%M:%S}] Tarama...", flush=True)
    t = get("/fapi/v1/ticker/24hr")
    if not isinstance(t, list):
        return
    semboller = [d["symbol"] for d in t
                 if d["symbol"].endswith("USDT") and float(d.get("quoteVolume", 0)) >= MIN_VOLUME]
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        sonuclar = list(ex.map(check, semboller))
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
           f"• <b>Net para ({LOOK} mum)</b>: {para(s['net'])} USDT\n"
           f"• <b>Normalin katı</b>: {s['kat']:.1f}x\n"
           f"• <b>Open interest</b>: %{s['oi']:+.1f}\n"
           f"• <b>Fiyat hareketi</b>: %{s['hareket']:+.2f}\n"
           f"• <b>Fiyat</b>: {s['fiyat']}\n"
           f"• <b>TF</b>: {TIMEFRAME}")
    print(f"{len(semboller)} sembol | {n} sinyal", flush=True)


def main():
    tg("✅ Para akışı botu (sade sürüm) başladı.")
    while True:
        t0 = time.time()
        try:
            scan()
        except Exception as e:
            print(f"Genel hata: {e}", flush=True)
        time.sleep(max(SCAN_SEC - (time.time() - t0), 1))


if __name__ == "__main__":
    main()
