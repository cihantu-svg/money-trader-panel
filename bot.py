import os, time, csv, requests
from datetime import datetime, timezone

TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT = os.getenv("TELEGRAM_CHAT_ID")
BASE = "https://fapi.binance.com"

MUM_YUZDE = 5          # 15dk mum open->high %
SMA_UST = 3            # mum low, 1h SMA100'ün en fazla %3 üstünde
SMA_ALT = 5            # mum low, 1h SMA100'ün en fazla %3 altında
MIN_HACIM = 1_000_000  # 24s USDT hacim
ARALIK = 60            # tarama aralığı (sn)
COOLDOWN = 4 * 1800    # aynı coine tekrar sinyal bekleme
RAPOR_SAATI = 21       # günlük rapor saati (UTC)
DOSYA = "sinyaller.csv"
ALANLAR = ["zaman", "tarih", "sembol", "fiyat", "mum_yuzde", "sma100_1h",
           "sma_mesafe", "hacim_24s", "degisim_24s", "en_yuksek_24s",
           "en_dusuk_24s", "sonuc_24s", "durum"]

oturum = requests.Session()
son = {}
kayitlar = []
son_rapor_gun = None

# ---------- yardımcılar ----------
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

def telegram_dosya():
    try:
        with open(DOSYA, "rb") as f:
            oturum.post(f"https://api.telegram.org/bot{TOKEN}/sendDocument",
                        data={"chat_id": CHAT}, files={"document": f}, timeout=30)
    except Exception as e:
        print("dosya hata:", e)

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
            kayitlar.append(r)
            son[r["sembol"]] = max(son.get(r["sembol"], 0), r["zaman"])

# ---------- 24 saat sonuç takibi ----------
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
            r["en_yuksek_24s"] = f"{yuksek:.2f}"
            r["en_dusuk_24s"] = f"{dusuk:.2f}"
            r["sonuc_24s"] = f"{sonuc:.2f}"
            r["durum"] = "YUKSELDI" if sonuc > 0 else "DUSTU"
            degisti = True
            telegram(f"📊 24s sonuç {r['sembol']}\n24s sonu: %{sonuc:.2f}\n"
                     f"En yüksek: %{yuksek:.2f} | En düşük: %{dusuk:.2f}\n{r['durum']}")
        except Exception as e:
            print("sonuc hata", r["sembol"], e)
        time.sleep(0.1)
    if degisti:
        csv_yaz()

# ---------- günlük rapor ----------
def gunluk_rapor():
    simdi = time.time()
    son24 = [r for r in kayitlar if simdi - r["zaman"] < 86400]
    bitenler = [r for r in kayitlar if r["durum"] != ""]
    yuk = sum(1 for r in bitenler if r["durum"] == "YUKSELDI")
    dus = len(bitenler) - yuk
    oran = yuk / len(bitenler) * 100 if bitenler else 0
    ort_yuksek = (sum(float(r["en_yuksek_24s"]) for r in bitenler) / len(bitenler)
                  if bitenler else 0)
    telegram(f"📅 Günlük rapor\nSon 24s sinyal: {len(son24)}\n"
             f"Sonuçlanan toplam: {len(bitenler)}\n"
             f"Yükselen: {yuk} | Düşen: {dus} | Oran: %{oran:.0f}\n"
             f"Ort. 24s en yüksek: %{ort_yuksek:.2f}")
    if kayitlar:
        csv_yaz()
        telegram_dosya()

# ---------- tarama ----------
def tara():
    tickerlar = {t["symbol"]: t for t in get("/fapi/v1/ticker/24hr")
                 if t["symbol"].endswith("USDT") and float(t["quoteVolume"]) >= MIN_HACIM}
    for s, t in tickerlar.items():
        try:
            if time.time() - son.get(s, 0) < COOLDOWN:
                continue
            mumlar = get("/fapi/v1/klines", symbol=s, interval="15m", limit=2)
            secilen = None
            for k in reversed(mumlar):  # önce açık mum, sonra kapanmış mum
                o, h = float(k[1]), float(k[2])
                if (h - o) / o * 100 >= MUM_YUZDE:
                    secilen = k
                    break
            if not secilen:
                continue

            o, h, l, c = (float(secilen[i]) for i in (1, 2, 3, 4))
            yuzde = (h - o) / o * 100

            k1 = get("/fapi/v1/klines", symbol=s, interval="1h", limit=101)[:-1]
            sma = sum(float(x[4]) for x in k1) / len(k1)
            mesafe = (l - sma) / sma * 100
            if mesafe > SMA_UST or mesafe < -SMA_ALT:
                continue

            fiyat = float(get("/fapi/v1/ticker/price", symbol=s)["price"])
            simdi = time.time()
            son[s] = simdi
            kayitlar.append({
                "zaman": simdi,
                "tarih": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"),
                "sembol": s, "fiyat": fiyat,
                "mum_yuzde": f"{yuzde:.2f}", "sma100_1h": f"{sma:.8g}",
                "sma_mesafe": f"{mesafe:.2f}",
                "hacim_24s": f"{float(t['quoteVolume']):.0f}",
                "degisim_24s": t["priceChangePercent"],
                "en_yuksek_24s": "", "en_dusuk_24s": "", "sonuc_24s": "", "durum": ""})
            csv_yaz()
            telegram(f"🚀 {s}\n15dk mum: +%{yuzde:.1f}\nFiyat: {fiyat}\n"
                     f"1h SMA100: {sma:.6g}\nMum low - SMA: %{mesafe:.2f}\n"
                     f"24s hacim: {float(t['quoteVolume'])/1e6:.1f}M | 24s değişim: %{t['priceChangePercent']}")
        except Exception as e:
            print(s, e)
        time.sleep(0.1)

# ---------- ana döngü ----------
csv_yukle()
while True:
    baslangic = time.time()
    try:
        tara()
        sonuc_kontrol()
        simdi = datetime.now(timezone.utc)
        if simdi.hour >= RAPOR_SAATI and son_rapor_gun != simdi.date():
            gunluk_rapor()
            son_rapor_gun = simdi.date()
    except Exception as e:
        print("hata:", e)
    time.sleep(max(0, ARALIK - (time.time() - baslangic)))
