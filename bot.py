import os, time, csv, requests
from datetime import datetime, timezone

TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT = os.getenv("TELEGRAM_CHAT_ID")
BASE = "https://fapi.binance.com"

# ---------------- AYARLAR ----------------
TARAMA_ARALIK = 180     # tarama sikligi (sn)
MIN_HACIM = 1_000_000   # 24s USDT hacim ALT SINIRI (coin sayisi sinirli degil)
ZAMAN_DILIMI = "15m"
COOLDOWN = 3600         # ayni coine tekrar sinyal bekleme (sn)

# Kirilim: TradingView Pine koduyla ayni
BO_LEN = 20             # onceki 20 mumun en yuksegi
BO_TAMPON = 0.5         # kirilim tamponu %
HACIM_CARPAN = 8.0      # hacim, 20 mumluk ortalamanin kac kati
RSI_AL = 60             # RSI alt siniri (MACD histogrami da > 0 olmali)

# SMA100 (15m) yakinligi
SMA_LEN = 100
SMA_ESIK = 5.0          # kirilim mumu kapanisi SMA100'e en fazla %5 uzakta olsun
                        # (ya da mum SMA100'u icinden gecsin = temas)

# Delta tetigi
DELTA_ORAN = 0.5        # sonraki yesil mumun deltasi >= kirilim deltasinin %50'si
BEKLEME_MUM = 3         # kirilimdan sonra en fazla kac mum beklensin
MUM_YESIL_SART = True   # tetik mumu ayrica yesil kapanmali (kapanis >= acilis)

DOSYA = "sinyaller_delta.csv"
ALANLAR = ["zaman", "tarih", "sembol", "fiyat", "kirilim_zaman", "stop",
           "kirilim_delta", "tetik_delta", "oran", "sma_mesafe", "hacim_x", "rsi",
           "en_yuksek_24s", "en_dusuk_24s", "sonuc_24s", "stop_vurdu", "durum"]

oturum = requests.Session()
kayitlar = []
son = {}            # sembol -> son sinyal zamani
gonderildi = set()  # "SEMBOL|kirilim_zaman" -> ayni kirilim icin tekrar sinyal yok
son_tarama = 0


# ---------- yardimcilar ----------
def get(path, **p):
    for _ in range(2):
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


def csv_yaz():
    with open(DOSYA, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=ALANLAR)
        w.writeheader()
        w.writerows(kayitlar)


def csv_yukle():
    if not os.path.exists(DOSYA):
        return
    with open(DOSYA, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            r["zaman"] = float(r["zaman"])
            r["fiyat"] = float(r["fiyat"])
            r["stop"] = float(r["stop"])
            kayitlar.append(r)
            son[r["sembol"]] = max(son.get(r["sembol"], 0), r["zaman"])
            gonderildi.add(f"{r['sembol']}|{r['kirilim_zaman']}")


def ema(vals, n):
    k = 2 / (n + 1)
    out = [vals[0]]
    for x in vals[1:]:
        out.append(x * k + out[-1] * (1 - k))
    return out


def rsi_seri(c, n=14):
    out = [50.0] * len(c)
    d = [c[i] - c[i - 1] for i in range(1, len(c))]
    ak = sum(max(x, 0) for x in d[:n]) / n
    ay = sum(max(-x, 0) for x in d[:n]) / n
    for i in range(n, len(c)):
        if i > n:
            x = d[i - 1]
            ak = (ak * (n - 1) + max(x, 0)) / n
            ay = (ay * (n - 1) + max(-x, 0)) / n
        out[i] = 100.0 if ay == 0 else 100 - 100 / (1 + ak / ay)
    return out


# ---------- sinyal mantigi ----------
def analiz(s):
    """Son kapanmis mumda 'kirilim + delta tetigi' var mi?"""
    k = get("/fapi/v1/klines", symbol=s, interval=ZAMAN_DILIMI, limit=250)[:-1]
    n = len(k)
    if n < 150:
        return None
    o = [float(x[1]) for x in k]
    h = [float(x[2]) for x in k]
    l = [float(x[3]) for x in k]
    c = [float(x[4]) for x in k]
    v = [float(x[5]) for x in k]
    delta = [2 * float(x[10]) - float(x[7]) for x in k]   # alici - satici (USDT)

    macd = [a - b for a, b in zip(ema(c, 12), ema(c, 26))]
    hist = [m - sg for m, sg in zip(macd, ema(macd, 9))]
    rsi = rsi_seri(c)

    j = n - 1   # son kapanmis mum = tetik mumu adayi
    for b in range(j - BEKLEME_MUM, j):     # kirilim mumu adayi
        # 1) Pine kirilimi
        direnc = max(h[b - BO_LEN:b])
        kirilim = c[b] > direnc * (1 + BO_TAMPON / 100) and c[b - 1] <= direnc
        vol_ort = sum(v[b - 19:b + 1]) / 20
        hacim_ok = vol_ort > 0 and v[b] > HACIM_CARPAN * vol_ort
        mom = rsi[b] > RSI_AL and hist[b] > 0
        if not (kirilim and hacim_ok and mom):
            continue
        D = delta[b]
        if D <= 0:
            continue

        # 2) SMA100 yakinligi / temasi
        sma = sum(c[b - SMA_LEN + 1:b + 1]) / SMA_LEN
        mesafe = (c[b] - sma) / sma * 100
        temas = l[b] <= sma <= h[b]
        if not (abs(mesafe) <= SMA_ESIK or temas):
            continue

        # 3) Delta tetigi: sonraki ilk guclu yesil mum
        for t in range(b + 1, j + 1):
            if c[t] < l[b]:                 # kirilim mumunun dibi kirildi -> iptal
                break
            guclu = delta[t] > 0 and delta[t] >= DELTA_ORAN * D
            yesil = c[t] >= o[t] or not MUM_YESIL_SART
            if guclu and yesil:
                if t == j:                  # tetik TAM SU AN (son mumda) oldu
                    return {"kirilim_zaman": int(k[b][0]), "stop": l[b],
                            "D": D, "Dt": delta[t], "oran": delta[t] / D,
                            "mesafe": mesafe, "hacim_x": v[b] / vol_ort, "rsi": rsi[b]}
                break                       # daha once tetiklenmis, zaten islendi
    return None


# ---------- sinyal kaydi ----------
def sinyal_ekle(s, t, fiyat, d):
    simdi = time.time()
    son[s] = simdi
    gonderildi.add(f"{s}|{d['kirilim_zaman']}")
    kayitlar.append({
        "zaman": simdi,
        "tarih": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"),
        "sembol": s, "fiyat": fiyat, "kirilim_zaman": d["kirilim_zaman"],
        "stop": d["stop"], "kirilim_delta": f"{d['D']:.0f}", "tetik_delta": f"{d['Dt']:.0f}",
        "oran": f"{d['oran']:.2f}", "sma_mesafe": f"{d['mesafe']:.2f}",
        "hacim_x": f"{d['hacim_x']:.1f}", "rsi": f"{d['rsi']:.0f}",
        "en_yuksek_24s": "", "en_dusuk_24s": "", "sonuc_24s": "", "stop_vurdu": "", "durum": ""})
    csv_yaz()
    stop_yuzde = (d["stop"] / fiyat - 1) * 100
    telegram(f"🚀 KIRILIM + DELTA {s}\n"
             f"Fiyat: {fiyat}\n"
             f"Kırılım hacmi: {d['hacim_x']:.1f}x | RSI: {d['rsi']:.0f}\n"
             f"Kırılım deltası: {d['D']/1e3:.0f}K | Tetik mum deltası: {d['Dt']/1e3:.0f}K "
             f"(%{d['oran']*100:.0f})\n"
             f"SMA100 mesafesi: %{d['mesafe']:.1f}\n"
             f"Stop (kırılım mumu dibi): {d['stop']} (%{stop_yuzde:.1f})\n"
             f"24s hacim: {float(t['quoteVolume'])/1e6:.1f}M")


# ---------- 24 saat sonuc takibi ----------
def sonuc_kontrol():
    degisti = False
    for r in kayitlar:
        if r["durum"] != "" or time.time() - r["zaman"] < 86400:
            continue
        try:
            ks = get("/fapi/v1/klines", symbol=r["sembol"], interval="15m",
                     startTime=int(r["zaman"] * 1000), limit=96)
            if not ks:
                continue
            f0 = r["fiyat"]
            yuksek = (max(float(x[2]) for x in ks) - f0) / f0 * 100
            dusuk = (min(float(x[3]) for x in ks) - f0) / f0 * 100
            sonuc = (float(ks[-1][4]) - f0) / f0 * 100
            stop_vurdu = min(float(x[3]) for x in ks) < r["stop"]
            r["en_yuksek_24s"] = f"{yuksek:.2f}"
            r["en_dusuk_24s"] = f"{dusuk:.2f}"
            r["sonuc_24s"] = f"{sonuc:.2f}"
            r["stop_vurdu"] = "EVET" if stop_vurdu else "HAYIR"
            r["durum"] = "BASARILI" if sonuc > 0 else "BASARISIZ"
            degisti = True
            telegram(f"📊 24s sonuç {r['sembol']}\n24s sonu: %{sonuc:.2f}\n"
                     f"En yüksek: %{yuksek:.2f} | En düşük: %{dusuk:.2f}\n"
                     f"Stop vuruldu mu: {r['stop_vurdu']} | {r['durum']}")
        except Exception as e:
            print("sonuc hata", r["sembol"], e)
        time.sleep(0.1)
    if degisti:
        csv_yaz()


# ---------- tarama (tum perp USDT coinleri, sayi siniri yok) ----------
def tara():
    tickerlar = [t for t in get("/fapi/v1/ticker/24hr")
                 if t["symbol"].endswith("USDT") and "_" not in t["symbol"]
                 and float(t["quoteVolume"]) >= MIN_HACIM]
    print(f"{len(tickerlar)} coin taranıyor")
    for t in tickerlar:
        s = t["symbol"]
        if time.time() - son.get(s, 0) < COOLDOWN:
            continue
        try:
            d = analiz(s)
            if d and f"{s}|{d['kirilim_zaman']}" not in gonderildi:
                sinyal_ekle(s, t, float(t["lastPrice"]), d)
        except Exception as e:
            print(s, e)
        time.sleep(0.1)


# ---------- ana dongu ----------
if __name__ == "__main__":
    csv_yukle()
    while True:
        try:
            if time.time() - son_tarama >= TARAMA_ARALIK:
                son_tarama = time.time()
                tara()
            sonuc_kontrol()
        except Exception as e:
            print("hata:", e)
        time.sleep(5)
