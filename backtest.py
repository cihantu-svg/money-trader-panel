import os, time, requests

TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT = os.getenv("TELEGRAM_CHAT_ID")
BASE = "https://fapi.binance.com"

MUM_YUZDE = 7         # 15dk mum boyu (yüksek-düşük) en az %
SMA_LEN = 100         # 4H SMA periyodu
TARAMA_ARALIK = 60    # tarama sıklığı (sn)
COOLDOWN = 1 * 3600   # aynı coinden tekrar mesaj için bekleme (4 saat)

oturum = requests.Session()
son_mesaj = {}
ilk_tarama = True

def log(*a):
    print(*a, flush=True)

def get(path, **p):
    for _ in range(2):
        r = oturum.get(BASE + path, params=p, timeout=15)
        if r.status_code in (418, 429):
            time.sleep(int(r.headers.get("Retry-After", 120)))
            continue
        if r.status_code == 451:
            raise RuntimeError("Binance 451: bu sunucu bölgesi engelli (Render bölgesini Frankfurt yap)")
        r.raise_for_status()
        return r.json()
    raise RuntimeError("Rate limit devam ediyor")

def telegram(msg):
    try:
        r = oturum.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                        data={"chat_id": CHAT, "text": msg}, timeout=15)
        if r.status_code != 200:
            log("TELEGRAM HATA:", r.status_code, r.text)
    except Exception as e:
        log("telegram hata:", e)

def tara():
    global ilk_tarama
    semboller = [t["symbol"] for t in get("/fapi/v1/ticker/24hr")
                 if t["symbol"].endswith("USDT") and "_" not in t["symbol"]]
    log(f"{len(semboller)} coin taranıyor")
    aday = 0
    for s in semboller:
        try:
            if time.time() - son_mesaj.get(s, 0) < COOLDOWN:
                continue
            k = get("/fapi/v1/klines", symbol=s, interval="15m", limit=3)[:-1]
            time.sleep(0.1)
            if not k:
                continue
            m = k[-1]
            acilis, yuksek, dusuk, kapanis = float(m[1]), float(m[2]), float(m[3]), float(m[4])
            boy = (yuksek - dusuk) / dusuk * 100
            if boy < MUM_YUZDE:
                continue
            aday += 1
            h4 = get("/fapi/v1/klines", symbol=s, interval="4h", limit=SMA_LEN)
            time.sleep(0.1)
            if len(h4) < SMA_LEN:
                continue
            sma = sum(float(x[4]) for x in h4) / SMA_LEN
            if dusuk <= sma <= yuksek:
                son_mesaj[s] = time.time()
                if ilk_tarama:
                    continue
                yon = "🟢 yeşil" if kapanis >= acilis else "🔴 kırmızı"
                telegram(f"📍 4H SMA100 TEMASI {s}\n"
                         f"15dk mum: %{boy:.1f} ({yon})\n"
                         f"Mum: {dusuk} - {yuksek}\n"
                         f"4H SMA100: {sma:.6g}")
        except Exception as e:
            log(s, e)
    log(f"tarama bitti | %{MUM_YUZDE}+ mum: {aday} coin")
    ilk_tarama = False

telegram("✅ SMA tarama botu başladı")
log("bot başladı")
while True:
    try:
        tara()
    except Exception as e:
        log("hata:", e)
    time.sleep(TARAMA_ARALIK)
