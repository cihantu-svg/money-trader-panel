import os, time, requests

TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT = os.getenv("TELEGRAM_CHAT_ID")
BASE = "https://fapi.binance.com"

MUM_YUZDE = 7          # 15dk açık mum open->high %
SMA_UST = 3            # mum low, SMA100'ün en fazla %3 üstünde
SMA_ALT = 3            # mum low, SMA100'ün en fazla %3 altında (sarkma payı)
MIN_HACIM = 1_000_000  # 24s USDT hacim
ARALIK = 60            # tarama aralığı (sn)
COOLDOWN = 4 * 3600    # aynı coine tekrar sinyal bekleme
son = {}
oturum = requests.Session()

def get(path, **p):
    for deneme in range(2):
        r = oturum.get(BASE + path, params=p, timeout=15)
        if r.status_code in (418, 429):
            bekle = int(r.headers.get("Retry-After", 120))
            print(f"Rate limit, {bekle} sn bekleniyor")
            time.sleep(bekle)
            continue
        r.raise_for_status()
        return r.json()
    raise RuntimeError("Rate limit devam ediyor")

def telegram(msg):
    try:
        oturum.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                    data={"chat_id": CHAT, "text": msg}, timeout=15)
    except Exception as e:
        print("telegram hata:", e)

def tara():
    semboller = [t["symbol"] for t in get("/fapi/v1/ticker/24hr")
                 if t["symbol"].endswith("USDT") and float(t["quoteVolume"]) >= MIN_HACIM]
    for s in semboller:
        try:
            if time.time() - son.get(s, 0) < COOLDOWN:
                continue
            k = get("/fapi/v1/klines", symbol=s, interval="15m", limit=1)[0]
            o, h, l, c = float(k[1]), float(k[2]), float(k[3]), float(k[4])
            yuzde = (h - o) / o * 100
            if yuzde < MUM_YUZDE:
                continue

            k1 = get("/fapi/v1/klines", symbol=s, interval="1h", limit=101)[:-1]
            sma = sum(float(x[4]) for x in k1) / len(k1)
            mesafe = (l - sma) / sma * 100
            if mesafe > SMA_UST or mesafe < -SMA_ALT:
                continue

            son[s] = time.time()
            telegram(f"🚀 {s}\n15dk mum: +%{yuzde:.1f}\nFiyat: {c}\n"
                     f"1h SMA100: {sma:.6g}\nMum low - SMA: %{mesafe:.2f}")
        except Exception as e:
            print(s, e)
        time.sleep(0.1)

while True:
    baslangic = time.time()
    try:
        tara()
    except Exception as e:
        print("hata:", e)
    time.sleep(max(0, ARALIK - (time.time() - baslangic)))
