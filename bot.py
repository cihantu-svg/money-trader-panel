import os, time, csv, requests
from datetime import datetime, timezone

TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT = os.getenv("TELEGRAM_CHAT_ID")
BASE = "https://fapi.binance.com"

# --- Ortak ---
TARAMA_ARALIK = 180    # tarama sıklığı (sn) -> 180 = 3 dakikada bir
MIN_HACIM = 1_000_000  # 24s USDT hacim (0 yaparsan hacim filtresi kalkar)
COOLDOWN = 1 * 3600    # aynı coine, aynı yönde tekrar sinyal bekleme
RAPOR_SAATI = 21       # günlük rapor saati (UTC)
ZAMAN_DILIMI = "15m"

# --- Kırılım ---
MUM_YUZDE = 5          # sinyal mumu boyu: AL için open->high, SAT için open->low %
BO_LEN = 20            # kırılım referans periyodu
BO_TAMPON = 0.5        # kırılım onay tamponu %
HACIM_CARPAN = 2.0     # hacim, 20 mumluk ortalamanın kaç katı
RSI_LEN = 14
RSI_AL = 60            # AL için RSI alt sınırı
RSI_SAT = 40           # SAT için RSI üst sınırı

# --- Ters hareket analizi (sadece CSV'ye yazar, sinyali etkilemez) ---
TERS_PENCERE_DK = 60   # sinyalden sonra kaç dakika izlensin
TERS_ESIK = 3.0        # sinyalin tersine en az bu kadar % hareket "ters hareket" sayılır
HIZLI_TERS_DK = 30     # raporda: ters hareket bu süre (dk) içinde olduysa "hızlı ters"

DOSYA = "sinyaller.csv"
ALANLAR = ["zaman", "tarih", "tip", "sembol", "fiyat", "mum_yuzde", "hacim_x",
           "rsi", "hacim_24s", "degisim_24s",
           "ters_dk", "max_ters_1s", "max_lehine_1s", "sonuc_1s",
           "en_yuksek_24s", "en_dusuk_24s", "sonuc_24s", "durum"]

oturum = requests.Session()
son = {}        # "SEMBOL|TIP" -> son sinyal zamanı
kayitlar = []
son_rapor_gun = None
son_tarama = 0

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
            anahtar = f"{r['sembol']}|{r['tip']}"
            son[anahtar] = max(son.get(anahtar, 0), r["zaman"])

# ---------- göstergeler ----------
def ema(vals, n):
    k = 2 / (n + 1)
    out = [vals[0]]
    for x in vals[1:]:
        out.append(x * k + out[-1] * (1 - k))
    return out

def rsi(vals, n):
    d = [vals[i] - vals[i - 1] for i in range(1, len(vals))]
    kazanc = [max(x, 0) for x in d]
    kayip = [max(-x, 0) for x in d]
    ort_k = sum(kazanc[:n]) / n
    ort_y = sum(kayip[:n]) / n
    for i in range(n, len(d)):
        ort_k = (ort_k * (n - 1) + kazanc[i]) / n
        ort_y = (ort_y * (n - 1) + kayip[i]) / n
    return 100.0 if ort_y == 0 else 100 - 100 / (1 + ort_k / ort_y)

def kirilim_hesapla(s):
    """Son KAPANMIŞ mumda kırılım sinyali var mı?"""
    k = get("/fapi/v1/klines", symbol=s, interval=ZAMAN_DILIMI, limit=300)[:-1]
    if len(k) < 60:
        return None
    o = [float(x[1]) for x in k]
    h = [float(x[2]) for x in k]
    l = [float(x[3]) for x in k]
    c = [float(x[4]) for x in k]
    v = [float(x[5]) for x in k]

    macd = [a - b for a, b in zip(ema(c, 12), ema(c, 26))]
    sinyal = ema(macd, 9)
    ml, sl = macd[-1], sinyal[-1]
    hist = ml - sl
    rsi_v = rsi(c, RSI_LEN)

    vol_sma = sum(v[-20:]) / 20
    hacim_ok = v[-1] > vol_sma * HACIM_CARPAN

    direnc = max(h[-BO_LEN - 1:-1])   # önceki 20 mumun en yükseği (repaint yok)
    destek = min(l[-BO_LEN - 1:-1])   # önceki 20 mumun en düşüğü

    kirilim_yukari = c[-1] > direnc * (1 + BO_TAMPON / 100) and c[-2] <= direnc
    kirilim_asagi = c[-1] < destek * (1 - BO_TAMPON / 100) and c[-2] >= destek

    mom_al = rsi_v > RSI_AL and hist > 0 and ml > sl
    mom_sat = rsi_v < RSI_SAT and hist < 0 and ml < sl

    mum_al = (h[-1] - o[-1]) / o[-1] * 100
    mum_sat = (o[-1] - l[-1]) / o[-1] * 100

    return {
        "al": kirilim_yukari and hacim_ok and mom_al and mum_al >= MUM_YUZDE,
        "sat": kirilim_asagi and hacim_ok and mom_sat and mum_sat >= MUM_YUZDE,
        "mum_al": mum_al, "mum_sat": mum_sat,
        "hacim_x": v[-1] / vol_sma if vol_sma else 0, "rsi": rsi_v}

# ---------- sinyal kaydı ----------
def sinyal_ekle(tip, s, t, fiyat, mum_yuzde, d):
    simdi = time.time()
    son[f"{s}|{tip}"] = simdi
    kayitlar.append({
        "zaman": simdi,
        "tarih": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"),
        "tip": tip, "sembol": s, "fiyat": fiyat,
        "mum_yuzde": f"{mum_yuzde:.2f}", "hacim_x": f"{d['hacim_x']:.1f}",
        "rsi": f"{d['rsi']:.0f}",
        "hacim_24s": f"{float(t['quoteVolume']):.0f}",
        "degisim_24s": t["priceChangePercent"],
        "ters_dk": "", "max_ters_1s": "", "max_lehine_1s": "", "sonuc_1s": "",
        "en_yuksek_24s": "", "en_dusuk_24s": "", "sonuc_24s": "", "durum": ""})
    csv_yaz()
    yon = "🚀 KIRILIM AL" if tip == "KIRILIM_AL" else "🔻 KIRILIM SAT"
    isaret = "+" if tip == "KIRILIM_AL" else "-"
    telegram(f"{yon} {s}\n{ZAMAN_DILIMI} mum: {isaret}%{mum_yuzde:.1f}\n"
             f"Hacim: {d['hacim_x']:.1f}x ortalama | RSI: {d['rsi']:.0f}\n"
             f"Fiyat: {fiyat}\n"
             f"24s hacim: {float(t['quoteVolume'])/1e6:.1f}M | 24s değişim: %{t['priceChangePercent']}")

# ---------- 1 saatlik ters hareket analizi (sadece CSV) ----------
def ters_kontrol():
    degisti = False
    for r in kayitlar:
        if r.get("max_ters_1s", "") != "" or time.time() - r["zaman"] < TERS_PENCERE_DK * 60:
            continue
        try:
            ks = get("/fapi/v1/klines", symbol=r["sembol"], interval="1m",
                     startTime=int(r["zaman"] * 1000), limit=TERS_PENCERE_DK)
            if not ks:
                continue
            f0 = r["fiyat"]
            al = r["tip"] == "KIRILIM_AL"
            max_ters, max_lehine, ilk_dk = 0.0, 0.0, ""
            for i, m in enumerate(ks, start=1):
                yuksek, dusuk = float(m[2]), float(m[3])
                ters = (f0 - dusuk) / f0 * 100 if al else (yuksek - f0) / f0 * 100
                lehine = (yuksek - f0) / f0 * 100 if al else (f0 - dusuk) / f0 * 100
                max_ters = max(max_ters, ters)
                max_lehine = max(max_lehine, lehine)
                if ilk_dk == "" and ters >= TERS_ESIK:
                    ilk_dk = i
            son_getiri = (float(ks[-1][4]) - f0) / f0 * 100 * (1 if al else -1)
            r["ters_dk"] = ilk_dk
            r["max_ters_1s"] = f"{max_ters:.2f}"
            r["max_lehine_1s"] = f"{max_lehine:.2f}"
            r["sonuc_1s"] = f"{son_getiri:.2f}"
            degisti = True
        except Exception as e:
            print("ters hata", r["sembol"], e)
        time.sleep(0.1)
    if degisti:
        csv_yaz()

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
            basarili = sonuc > 0 if r["tip"] == "KIRILIM_AL" else sonuc < 0
            r["en_yuksek_24s"] = f"{yuksek:.2f}"
            r["en_dusuk_24s"] = f"{dusuk:.2f}"
            r["sonuc_24s"] = f"{sonuc:.2f}"
            r["durum"] = "BASARILI" if basarili else "BASARISIZ"
            degisti = True
            telegram(f"📊 24s sonuç [{r['tip']}] {r['sembol']}\n24s sonu: %{sonuc:.2f}\n"
                     f"En yüksek: %{yuksek:.2f} | En düşük: %{dusuk:.2f}\n{r['durum']}")
        except Exception as e:
            print("sonuc hata", r["sembol"], e)
        time.sleep(0.1)
    if degisti:
        csv_yaz()

# ---------- günlük rapor ----------
def gunluk_rapor():
    simdi = time.time()
    metin = "📅 Günlük rapor\n"
    for tip in ("KIRILIM_AL", "KIRILIM_SAT"):
        tum = [r for r in kayitlar if r["tip"] == tip]
        son24 = [r for r in tum if simdi - r["zaman"] < 86400]
        bitenler = [r for r in tum if r["durum"] != ""]
        basarili = sum(1 for r in bitenler if r["durum"] == "BASARILI")
        oran = basarili / len(bitenler) * 100 if bitenler else 0
        if tip == "KIRILIM_AL":
            ort = sum(float(r["en_yuksek_24s"]) for r in bitenler) / len(bitenler) if bitenler else 0
        else:
            ort = sum(-float(r["en_dusuk_24s"]) for r in bitenler) / len(bitenler) if bitenler else 0
        olculen = [r for r in tum if r.get("max_ters_1s", "") != ""]
        hizli = [r for r in olculen if r.get("ters_dk", "") != "" and float(r["ters_dk"]) <= HIZLI_TERS_DK]
        metin += (f"\n{tip}\nSon 24s sinyal: {len(son24)}\n"
                  f"Sonuçlanan: {len(bitenler)} | Başarılı: {basarili} | Oran: %{oran:.0f}\n"
                  f"Ort. lehine max hareket: %{ort:.2f}\n"
                  f"Hızlı ters (ilk {HIZLI_TERS_DK}dk %{TERS_ESIK:g}+): {len(hizli)}/{len(olculen)}\n")
    telegram(metin)
    if kayitlar:
        csv_yaz()
        telegram_dosya()

# ---------- tarama (tüm perp USDT coinleri) ----------
def tara():
    tickerlar = [t for t in get("/fapi/v1/ticker/24hr")
                 if t["symbol"].endswith("USDT") and "_" not in t["symbol"]
                 and float(t["quoteVolume"]) >= MIN_HACIM]
    print(f"{len(tickerlar)} coin taranıyor")
    for t in tickerlar:
        s = t["symbol"]
        try:
            d = kirilim_hesapla(s)
            if not d:
                continue
            fiyat = float(t["lastPrice"])
            simdi = time.time()
            if d["al"] and simdi - son.get(f"{s}|KIRILIM_AL", 0) >= COOLDOWN:
                sinyal_ekle("KIRILIM_AL", s, t, fiyat, d["mum_al"], d)
            if d["sat"] and simdi - son.get(f"{s}|KIRILIM_SAT", 0) >= COOLDOWN:
                sinyal_ekle("KIRILIM_SAT", s, t, fiyat, d["mum_sat"], d)
        except Exception as e:
            print(s, e)
        time.sleep(0.1)

# ---------- ana döngü ----------
csv_yukle()
while True:
    try:
        if time.time() - son_tarama >= TARAMA_ARALIK:
            son_tarama = time.time()
            tara()
        ters_kontrol()
        sonuc_kontrol()
        simdi = datetime.now(timezone.utc)
        if simdi.hour >= RAPOR_SAATI and son_rapor_gun != simdi.date():
            gunluk_rapor()
            son_rapor_gun = simdi.date()
    except Exception as e:
        print("hata:", e)
    time.sleep(5)
