import os, sys, time, csv, requests
from bisect import bisect_left
from datetime import datetime, timezone

BASE = "https://fapi.binance.com"

GUN = 90                  # kaç günlük geçmiş test edilsin
MIN_HACIM = 1_000_000     # 24s USDT hacim alt sınırı
TOP_N = 400               # hacme göre en fazla kaç coin
MUM_YUZDE = 7             # 15dk mum boyu (yüksek-düşük) en az %
OB_YAKIN = 5.0            # 4H OB bölgesine max yakınlık % (0 = sadece temas)
SON_OB = 5                # indikatör gibi: en yeni 5 iç OB
ATLA_MUM = 16             # aynı coinde iki olay arası min 15dk mum (4 saat)
ONAY_MUM = 24             # 5dk dönüş işareti için max bekleme (2 saat)
YAPI_MUM = 6              # yapı bozulması: önceki 6 5dk mumun ucu kırılsın
TUT_MUM = 576             # işlemde max kalma (576 x 5dk = 48 saat)
SL = 5.0                  # stop %
TP1 = 10.0                # 1. hedef % (pozisyonun yarısı)
TP2 = 20.0                # 2. hedef % (kalan yarı)
UCRET = 0.1               # gidiş-dönüş komisyon %
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
    k = get("/fapi/v1/klines", symbol=s, interval="4h", limit=1500)
    time.sleep(0.5)
    return k[:-1]     # kapanmamış son 4H mumu at

# ---------- Pine'daki iç OB mantığı (4H) ----------
class Pv:
    def __init__(self):
        self.cur, self.last, self.crossed, self.idx = None, None, False, 0

def esit_degil(a, b):
    return True if (a is None or b is None) else a != b

def ob_gecmisi(k4):
    """Her 4H mum kapanışından sonra aktif en yeni SON_OB iç OB'yi döndürür.
    Her OB: (ust, alt, yon)  yon: +1 yükseliş (alt tarafta), -1 düşüş (üst tarafta)"""
    n = len(k4)
    T = [x[0] for x in k4]
    H = [float(x[2]) for x in k4]
    L = [float(x[3]) for x in k4]
    C = [float(x[4]) for x in k4]
    tr = [H[0] - L[0]] + [max(H[i] - L[i], abs(H[i] - C[i - 1]), abs(L[i] - C[i - 1])) for i in range(1, n)]
    atr = [None] * n
    if n >= 200:
        a = sum(tr[:200]) / 200
        atr[199] = a
        for i in range(200, n):
            a = (a * 199 + tr[i]) / 200
            atr[i] = a
    PH, PL = [], []
    for i in range(n):
        vol = atr[i] is not None and (H[i] - L[i]) >= 2 * atr[i]
        PH.append(L[i] if vol else H[i])
        PL.append(H[i] if vol else L[i])

    ih, il, sh, sl = Pv(), Pv(), Pv(), Pv()
    leg, onceki = {5: 0, 50: 0}, {5: 0, 50: 0}
    prev_ih = prev_il = None
    obs, snaps = [], []
    for i in range(n):
        for size, hp, lp in ((50, sh, sl), (5, ih, il)):
            if i >= size:
                if H[i - size] > max(H[i - size + 1:i + 1]):
                    leg[size] = 0
                elif L[i - size] < min(L[i - size + 1:i + 1]):
                    leg[size] = 1
            degisim = leg[size] - onceki[size]
            onceki[size] = leg[size]
            if degisim == 1:
                lp.last, lp.cur, lp.crossed, lp.idx = lp.cur, L[i - size], False, i - size
            elif degisim == -1:
                hp.last, hp.cur, hp.crossed, hp.idx = hp.cur, H[i - size], False, i - size
        if i > 0:
            # yükseliş yapı kırılımı -> yükseliş OB (pivot tepeden bugüne en düşük parsed low)
            yukari = (ih.cur is not None and prev_ih is not None
                      and C[i] > ih.cur and C[i - 1] <= prev_ih)
            if yukari and not ih.crossed and esit_degil(ih.cur, sh.cur):
                ih.crossed = True
                a = PL[ih.idx:i]
                if a:
                    j = ih.idx + a.index(min(a))
                    if len(obs) >= 100:
                        obs.pop()
                    obs.insert(0, (PH[j], PL[j], 1))
            # düşüş yapı kırılımı -> düşüş OB (pivot dipten bugüne en yüksek parsed high)
            asagi = (il.cur is not None and prev_il is not None
                     and C[i] < il.cur and C[i - 1] >= prev_il)
            if asagi and not il.crossed and esit_degil(il.cur, sl.cur):
                il.crossed = True
                a = PH[il.idx:i]
                if a:
                    j = il.idx + a.index(max(a))
                    if len(obs) >= 100:
                        obs.pop()
                    obs.insert(0, (PH[j], PL[j], -1))
        prev_ih, prev_il = ih.cur, il.cur
        # mitigation (High/Low): düşüş OB'nin üstüne fitil, yükseliş OB'nin altına fitil -> OB silinir
        obs = [o for o in obs if not ((o[2] == -1 and H[i] > o[0]) or (o[2] == 1 and L[i] < o[1]))]
        snaps.append(tuple(obs[:SON_OB]))
    return T, snaps

def ob_kontrol(s):
    """Grafikle karşılaştırmak için: son 4H kapanışındaki aktif OB bölgeleri"""
    k4 = mumlar_4h(s)
    T, snaps = ob_gecmisi(k4)
    yaz(f"{s} 4H aktif iç OB'ler (en yeni önce):")
    for ust, alt, yon in snaps[-1]:
        yaz(f"  {'DÜŞÜŞ (kırmızı)' if yon == -1 else 'YÜKSELİŞ (mavi)'}: {alt:.6g} - {ust:.6g}")
    if not snaps[-1]:
        yaz("  (aktif OB yok)")

# ---------- olay bulma ----------
def bes_dk(s, t):
    k = get("/fapi/v1/klines", symbol=s, interval="5m",
            startTime=t + 10 * 60 * 1000, limit=TUT_MUM + ONAY_MUM + 10)
    time.sleep(0.3)
    if len(k) < 30:
        return None
    return {"o": [float(x[1]) for x in k], "h": [float(x[2]) for x in k],
            "l": [float(x[3]) for x in k], "c": [float(x[4]) for x in k]}

def olaylar_bul(s, k15, k4):
    if len(k4) < 60:
        return []
    h4_t, snaps = ob_gecmisi(k4)
    olaylar, son_i = [], -999
    for i in range(1, len(k15)):
        o, h, l, c = (float(k15[i][1]), float(k15[i][2]), float(k15[i][3]), float(k15[i][4]))
        if (h - l) / l * 100 < MUM_YUZDE or i - son_i < ATLA_MUM:
            continue
        t = k15[i][0]
        n = bisect_left(h4_t, t // H4 * H4)    # bu 4H mumdan önce kapanmış 4H mum sayısı
        if n < 1:
            continue
        adaylar = []
        for ust, alt, yon in snaps[n - 1]:
            if yon == -1:       # düşüş OB üstte: yeşil mumla yaklaşılmış, OB'yi delmemiş olmalı
                if c <= o or h > ust:
                    continue
                d = 0.0 if h >= alt else (alt - h) / h * 100
            else:               # yükseliş OB altta: kırmızı mumla yaklaşılmış, OB'yi delmemiş olmalı
                if c >= o or l < alt:
                    continue
                d = 0.0 if l <= ust else (l - ust) / l * 100
            if d <= OB_YAKIN:
                adaylar.append((d, yon, ust, alt))
        if not adaylar:
            continue
        d, yon, ust, alt = min(adaylar)
        son_i = i
        b = bes_dk(s, t)
        if b is None:
            continue
        olaylar.append({"sembol": s, "t": t, "yon": "SHORT" if yon == -1 else "LONG",
                        "kapanis": c, "mum": (h - l) / l * 100, "ob_mesafe": d,
                        "ob_ust": ust, "ob_alt": alt, "b": b})
    return olaylar

# ---------- işlem simülasyonu ----------
def cikis_bul(b, bas, giris, yon, be):
    sg = 1 if yon == "LONG" else -1
    son = min(bas + TUT_MUM, len(b["c"]))
    if bas >= son:
        return None
    stop, tp1_alindi = -SL, False
    for j in range(bas, son):
        fav = b["h"][j] if sg == 1 else b["l"][j]
        adv = b["l"][j] if sg == 1 else b["h"][j]
        fav_p = sg * (fav - giris) / giris * 100
        adv_p = sg * (adv - giris) / giris * 100
        if adv_p <= stop:                         # önce stop kontrolü (kötümser)
            if tp1_alindi:
                return 0.5 * TP1 + 0.5 * stop, "TP1_STOP"
            return stop, "STOP"
        if fav_p >= TP2:
            return 0.5 * TP1 + 0.5 * TP2, "TP2"
        if not tp1_alindi and fav_p >= TP1:
            tp1_alindi = True
            if be:
                stop = 0.0                        # TP1 sonrası stop girişe çekilir
    son_p = sg * (b["c"][son - 1] - giris) / giris * 100
    if tp1_alindi:
        return 0.5 * TP1 + 0.5 * son_p, "TP1_SURE"
    return son_p, "SURE"

def islem(ol, giris_tipi, be):
    b, yon = ol["b"], ol["yon"]
    if giris_tipi == "15dk":
        giris, bas = ol["kapanis"], 1
    else:   # 5dk yapı bozulması: önceki 6 5dk mumun ucunu dönüş yönünde kapanışla kır
        giris, bas = None, None
        for j in range(3, min(3 + ONAY_MUM, len(b["c"]))):
            bs = max(0, j - YAPI_MUM)
            if yon == "SHORT" and b["c"][j] < b["o"][j] and b["c"][j] < min(b["l"][bs:j]):
                giris, bas = b["c"][j], j + 1
                break
            if yon == "LONG" and b["c"][j] > b["o"][j] and b["c"][j] > max(b["h"][bs:j]):
                giris, bas = b["c"][j], j + 1
                break
        if giris is None:
            return None
    r = cikis_bul(b, bas, giris, yon, be)
    if r is None:
        return None
    getiri, tip = r
    return {"sembol": ol["sembol"], "yon": yon, "giris_tipi": giris_tipi, "be": be,
            "tarih": datetime.fromtimestamp(ol["t"] / 1000, timezone.utc).strftime("%Y-%m-%d %H:%M"),
            "mum_yuzde": round(ol["mum"], 1), "ob_mesafe": round(ol["ob_mesafe"], 2),
            "ob_alt": ol["ob_alt"], "ob_ust": ol["ob_ust"], "giris": giris,
            "sonuc": tip, "getiri": round(getiri - UCRET, 2)}

def main():
    kontrol = os.getenv("OB_KONTROL") or (sys.argv[1] if len(sys.argv) > 1 else None)
    if kontrol:                       # sadece OB bölgelerini yazdır (grafikle karşılaştırmak için)
        ob_kontrol(kontrol)
        telegrama_gonder("🔎 OB kontrol", "")
        return

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

    yaz(f"Toplam olay: {len(olaylar)} | SHORT: {sum(1 for o in olaylar if o['yon']=='SHORT')}"
        f" | LONG: {sum(1 for o in olaylar if o['yon']=='LONG')}")
    yaz(f"OB yakınlık<=%{OB_YAKIN} | SL %{SL} | TP1 %{TP1} (yarı) | TP2 %{TP2} (yarı) | {GUN} gün")
    yaz(f"\n{'giris':>9} {'BE':>3} {'yon':>6} {'islem':>6} {'tp1+%':>6} {'tp2%':>5} {'stop%':>6} {'ort.get%':>9} {'toplam%':>9}")

    tum = []
    for gt in ("15dk", "5dk_yapi"):
        for be in (False, True):
            rows = [r for o in olaylar if (r := islem(o, gt, be))]
            tum += rows
            for yf in ("hepsi", "SHORT", "LONG"):
                x = [r for r in rows if yf == "hepsi" or r["yon"] == yf]
                m = len(x)
                if m == 0:
                    yaz(f"{gt:>9} {'E' if be else 'H':>3} {yf:>6} {0:>6}")
                    continue
                tp1o = sum(1 for r in x if r["sonuc"] in ("TP1_STOP", "TP1_SURE", "TP2")) / m * 100
                tp2o = sum(1 for r in x if r["sonuc"] == "TP2") / m * 100
                stopo = sum(1 for r in x if r["sonuc"] == "STOP") / m * 100
                yaz(f"{gt:>9} {'E' if be else 'H':>3} {yf:>6} {m:>6} {tp1o:>6.0f} {tp2o:>5.0f} {stopo:>6.0f} "
                    f"{sum(r['getiri'] for r in x)/m:>9.2f} {sum(r['getiri'] for r in x):>9.1f}")
    yaz("\nBE: E = TP1 sonrası stop girişe çekilir, H = stop %5'te kalır")

    if tum:
        with open("backtest_ob_islemler.csv", "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(tum[0].keys()))
            w.writeheader()
            w.writerows(tum)
        print("\nTüm işlemler: backtest_ob_islemler.csv")

    telegrama_gonder("📊 Backtest (4H OB tepki) bitti", "backtest_ob_islemler.csv")

if __name__ == "__main__":
    main()
    if os.getenv("RENDER"):   # Render'da program çıkarsa servis yeniden başlar ve backtest'i baştan çalıştırır
        print("Bitti. Servisi durdurabilirsin.")
        while True:
            time.sleep(3600)
