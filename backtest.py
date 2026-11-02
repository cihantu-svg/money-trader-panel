import os, time, csv, requests
from bisect import bisect_left
from datetime import datetime, timezone

BASE = "https://fapi.binance.com"

GUN = 30                  # kaç günlük geçmiş test edilsin
MIN_HACIM = 1_000_000     # 24s USDT hacim alt sınırı (tarama botuyla aynı)
TOP_N = 400               # hacme göre en fazla kaç coin
MUM_YUZDE = 7             # 15dk mum boyu (yüksek-düşük) en az %
SMA_LEN = 100             # 4H SMA periyodu
ATLA_MUM = 16             # aynı coinde iki olay arası min 15dk mum (16 = 4 saat)
ONAY_MUM = 24             # 5dk dönüş işareti için max bekleme (24 mum = 2 saat)
YAPI_MUM = 6              # yapı bozulması: önceki kaç 5dk mumun ucu kırılsın (6 = 30 dk)
TUT_MUM = 288             # işlemde max kalma (288 x 5dk = 24 saat)
UCRET = 0.1               # gidiş-dönüş komisyon %
STOPLAR = [3, 5]          # stop % (girişten)
HEDEFLER = [5, 10]        # hedef % (girişten)
H4 = 4 * 3600 * 1000

oturum = requests.Session()

TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT = os.getenv("TELEGRAM_CHAT_ID")
satirlar = []

def yaz(m):
    print(m)
    satirlar.append(m)

def telegram(msg):
    try:
        r = oturum.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                        data={"chat_id": CHAT, "text": msg, "parse_mode": "HTML"}, timeout=15)
        if r.status_code != 200:
            print("TELEGRAM HATA:", r.status_code, r.text)
    except Exception as e:
        print("telegram hata:", e)

def telegram_dosya(yol):
    try:
        with open(yol, "rb") as f:
            oturum.post(f"https://api.telegram.org/bot{TOKEN}/sendDocument",
                        data={"chat_id": CHAT}, files={"document": f}, timeout=60)
    except Exception as e:
        print("dosya hata:", e)

def telegrama_gonder(baslik, csv_yolu):
    if not (TOKEN and CHAT):
        print("TELEGRAM_TOKEN / TELEGRAM_CHAT_ID tanımlı değil, Telegram'a gönderilmedi")
        return
    parca = ""
    for satir in satirlar:
        if len(parca) + len(satir) > 3500:
            telegram("<pre>" + parca + "</pre>")
            parca = ""
        parca += satir + "\n"
    telegram(baslik + "\n<pre>" + parca + "</pre>")
    if os.path.exists(csv_yolu):
        telegram_dosya(csv_yolu)


def get(path, **p):
    for _ in range(2):
        r = oturum.get(BASE + path, params=p, timeout=15)
        if r.status_code in (418, 429):
            time.sleep(int(r.headers.get("Retry-After", 60)))
            continue
        r.raise_for_status()
        return r.json()
    raise RuntimeError("Rate limit devam ediyor")

def mumlar_15dk(s):
    bitis = int(time.time() * 1000)
    hedef = bitis - GUN * 86400 * 1000
    out = []
    while bitis > hedef:
        k = get("/fapi/v1/klines", symbol=s, interval="15m", limit=1500, endTime=bitis)
        time.sleep(0.5)
        if not k:
            break
        out = k + out
        bitis = k[0][0] - 1
        if len(k) < 1500:
            break
    return out[:-1]   # kapanmamış son mumu at

def mumlar_4h(s):
    k = get("/fapi/v1/klines", symbol=s, interval="4h", limit=(GUN + 20) * 6)
    time.sleep(0.5)
    return k

def bes_dk(s, t):
    """Dokunan 15dk mumun son 5dk'sından başlayarak ~25 saatlik 5dk mum"""
    k = get("/fapi/v1/klines", symbol=s, interval="5m",
            startTime=t + 10 * 60 * 1000, limit=TUT_MUM + ONAY_MUM + 10)
    time.sleep(0.3)
    if len(k) < 30:
        return None
    return {"o": [float(x[1]) for x in k], "h": [float(x[2]) for x in k],
            "l": [float(x[3]) for x in k], "c": [float(x[4]) for x in k],
            "d": [2 * float(x[10]) - float(x[7]) for x in k]}

def olaylar_bul(s, k15, h4):
    """%7+ 15dk mum 4H SMA100'e dokunduysa olay say"""
    if len(h4) < SMA_LEN:
        return []
    h4_t = [x[0] for x in h4]
    cum = [0.0]
    for x in h4:
        cum.append(cum[-1] + float(x[4]))
    olaylar, son_i = [], -999
    for i in range(1, len(k15)):
        h, l, c = float(k15[i][2]), float(k15[i][3]), float(k15[i][4])
        if (h - l) / l * 100 < MUM_YUZDE or i - son_i < ATLA_MUM:
            continue
        t = k15[i][0]
        n = bisect_left(h4_t, t // H4 * H4)     # bu 4H mumdan önce kapanmış 4H mum sayısı
        if n < SMA_LEN - 1:
            continue
        sma = (cum[n] - cum[n - (SMA_LEN - 1)] + c) / SMA_LEN   # son 99 kapanmış + şu anki fiyat
        if not (l <= sma <= h):
            continue
        son_i = i
        yon = "SHORT" if float(k15[i - 1][4]) < sma else "LONG"   # alttan geliyorsa direnç, üstten geliyorsa destek
        b = bes_dk(s, t)
        if b is None:
            continue
        olaylar.append({"sembol": s, "t": t, "yon": yon, "kapanis": c, "sma": sma,
                        "mum": (h - l) / l * 100, "b": b})
    return olaylar

def cikis_bul(b, bas, giris, yon, stop, hedef):
    if yon == "SHORT":
        st, hd = giris * (1 + stop / 100), giris * (1 - hedef / 100)
    else:
        st, hd = giris * (1 - stop / 100), giris * (1 + hedef / 100)
    son = min(bas + TUT_MUM, len(b["c"]))
    if bas >= son:
        return None
    for j in range(bas, son):
        if yon == "SHORT":
            if b["h"][j] >= st:                  # önce stop kontrolü (kötümser)
                return st, "STOP"
            if b["l"][j] <= hd:
                return hd, "HEDEF"
        else:
            if b["l"][j] <= st:
                return st, "STOP"
            if b["h"][j] >= hd:
                return hd, "HEDEF"
    return b["c"][son - 1], "SURE"

def islem(ol, giris_tipi, stop, hedef):
    b, yon = ol["b"], ol["yon"]
    if giris_tipi == "15dk":
        giris, bas = ol["kapanis"], 1
    else:
        giris, bas = None, None
        for j in range(3, min(3 + ONAY_MUM, len(b["c"]))):
            if giris_tipi == "5dk_mum":
                # basit: beklenen yönde mum + delta aynı yönde + önceki 5dk mumun ucunu kır
                if yon == "SHORT" and b["c"][j] < b["o"][j] and b["d"][j] < 0 and b["c"][j] < b["l"][j - 1]:
                    giris, bas = b["c"][j], j + 1
                    break
                if yon == "LONG" and b["c"][j] > b["o"][j] and b["d"][j] > 0 and b["c"][j] > b["h"][j - 1]:
                    giris, bas = b["c"][j], j + 1
                    break
            else:
                # yapı bozulması (CHoCH benzeri): önceki 6 mumun dibini/tepesini kapanışla kır
                bs = max(0, j - YAPI_MUM)
                if yon == "SHORT" and b["c"][j] < b["o"][j] and b["c"][j] < min(b["l"][bs:j]):
                    giris, bas = b["c"][j], j + 1
                    break
                if yon == "LONG" and b["c"][j] > b["o"][j] and b["c"][j] > max(b["h"][bs:j]):
                    giris, bas = b["c"][j], j + 1
                    break
        if giris is None:
            return None
    r = cikis_bul(b, bas, giris, yon, stop, hedef)
    if r is None:
        return None
    cikis, tip = r
    getiri = ((giris - cikis) if yon == "SHORT" else (cikis - giris)) / giris * 100 - UCRET
    return {"sembol": ol["sembol"], "yon": yon, "giris_tipi": giris_tipi, "stop": stop,
            "hedef": hedef, "tarih": datetime.fromtimestamp(ol["t"] / 1000, timezone.utc).strftime("%Y-%m-%d %H:%M"),
            "mum_yuzde": round(ol["mum"], 1), "giris": giris, "cikis": cikis,
            "sonuc": tip, "getiri": round(getiri, 2)}

def main():
    ts = [t for t in get("/fapi/v1/ticker/24hr")
          if t["symbol"].endswith("USDT") and "_" not in t["symbol"]
          and float(t["quoteVolume"]) >= MIN_HACIM]
    ts.sort(key=lambda t: float(t["quoteVolume"]), reverse=True)
    semboller = [t["symbol"] for t in ts[:TOP_N]]

    olaylar = []
    for n, s in enumerate(semboller, 1):
        try:
            olaylar += olaylar_bul(s, mumlar_15dk(s), mumlar_4h(s))
            print(f"{n}/{len(semboller)} {s} | olay: {len(olaylar)}")
        except Exception as e:
            print(s, e)

    yaz(f"\nToplam olay: {len(olaylar)} | SHORT: {sum(1 for o in olaylar if o['yon']=='SHORT')}"
          f" | LONG: {sum(1 for o in olaylar if o['yon']=='LONG')}")

    tum = []
    yaz(f"\n{'giris':>9} {'stop':>5} {'hedef':>6} {'yon':>6} {'islem':>6} {'hedef%':>7} {'stop%':>6} {'basabas%':>9} {'ort.get%':>9} {'toplam%':>9}")
    for gt in ("15dk", "5dk_mum", "5dk_yapi"):
        for sp in STOPLAR:
            for hd in HEDEFLER:
                rows = [r for o in olaylar if (r := islem(o, gt, sp, hd))]
                tum += rows
                for yf in ("hepsi", "SHORT", "LONG"):
                    x = [r for r in rows if yf == "hepsi" or r["yon"] == yf]
                    n = len(x)
                    if n == 0:
                        yaz(f"{gt:>9} {sp:>5} {hd:>6} {yf:>6} {0:>6}")
                        continue
                    ho = sum(1 for r in x if r["sonuc"] == "HEDEF") / n * 100
                    so = sum(1 for r in x if r["sonuc"] == "STOP") / n * 100
                    be = (sp + UCRET) / (hd + sp) * 100
                    yaz(f"{gt:>9} {sp:>5} {hd:>6} {yf:>6} {n:>6} {ho:>7.0f} {so:>6.0f} {be:>9.0f} "
                          f"{sum(r['getiri'] for r in x)/n:>9.2f} {sum(r['getiri'] for r in x):>9.1f}")

    if tum:
        with open("backtest_sma_islemler.csv", "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(tum[0].keys()))
            w.writeheader()
            w.writerows(tum)
        print("\nTüm işlemler: backtest_sma_islemler.csv")

    telegrama_gonder("📊 Backtest (4H SMA temas) bitti", "backtest_sma_islemler.csv")

if __name__ == "__main__":
    main()
    if os.getenv("RENDER"):   # Render'da program çıkarsa servis yeniden başlar ve backtest'i baştan çalıştırır
        print("Bitti. Servisi durdurabilirsin.")
        while True:
            time.sleep(3600)
