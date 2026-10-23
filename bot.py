import time
import json
import csv
import os
import requests
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor

# ---- AYARLAR ----
MIN_HACIM_USD = float(os.environ.get("MIN_HACIM_USD", "1000000"))
MIN_YUKSELIS = float(os.environ.get("MIN_YUKSELIS", "10"))
TARAMA_SANIYE = 3 * 60      # 3 dakikada bir tara
DEGERLENDIRME_MS = 24 * 3600 * 1000
GUN_SONU_SAAT = 23          # her gün bu saatten sonra rapor + CSV gönderir (sunucu saati)

# Render'da Environment kısmına yaz, ya da buraya direkt yapıştır
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "BURAYA_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "BURAYA_CHAT_ID")

JSON_DOSYA = "futures_sinyaller.json"
CSV_DOSYA = "futures_sonuclar.csv"
# -----------------------

BASE = "https://fapi.binance.com"

ALANLAR = [
    "zaman", "sembol", "giris", "acilis", "tepe", "yukselis", "anlik", "geri_cekilme",
    "mum_dk", "mum_hacim_usd", "alim_orani", "islem_sayisi", "hacim24_usd",
    "hacim_payi", "degisim24", "btc24",
    "son_fiyat24", "degisim_24s", "max_yuk", "max_dus", "sonuc",
]

sinyaller = []
if os.path.exists(JSON_DOSYA):
    with open(JSON_DOSYA, "r", encoding="utf-8") as f:
        sinyaller = json.load(f)
bildirilen = {(s["sembol"], s["mum_acilis_ms"]) for s in sinyaller}
durum = {"son_gun_sonu": None}


# ---------- TELEGRAM ----------
def telegram_hazir():
    if "BURAYA" in TELEGRAM_TOKEN or "BURAYA" in TELEGRAM_CHAT_ID:
        print("⚠️ Telegram token/chat id girilmemiş, mesaj gönderilmedi")
        return False
    return True


def telegram_mesaj(metin):
    if not telegram_hazir():
        return
    for i in range(0, len(metin), 4000):
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                data={"chat_id": TELEGRAM_CHAT_ID, "text": metin[i:i + 4000]},
                timeout=15,
            )
            if r.status_code != 200:
                print("⚠️ Telegram hatası:", r.status_code, r.text[:200])
        except Exception as e:
            print("Telegram hatası:", e)


def telegram_dosya(yol):
    if not telegram_hazir() or not os.path.exists(yol):
        return
    try:
        with open(yol, "rb") as f:
            r = requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendDocument",
                data={"chat_id": TELEGRAM_CHAT_ID},
                files={"document": f},
                timeout=30,
            )
        if r.status_code != 200:
            print("⚠️ Telegram dosya hatası:", r.status_code, r.text[:200])
    except Exception as e:
        print("Telegram dosya hatası:", e)


def yaz(metin):
    print(metin)
    telegram_mesaj(metin)


# ---------- TARAMA ----------
def kaydet():
    with open(JSON_DOSYA, "w", encoding="utf-8") as f:
        json.dump(sinyaller, f, ensure_ascii=False)


def mum_kontrol(t):
    sembol = t["symbol"]
    try:
        r = requests.get(
            f"{BASE}/fapi/v1/klines",
            params={"symbol": sembol, "interval": "15m", "limit": 1},
            timeout=10,
        )
        if r.status_code in (418, 429):
            time.sleep(10)
            return None
        k = r.json()[0]
        acilis = float(k[1])
        tepe = float(k[2])
        simdiki = float(k[4])
        if acilis <= 0 or tepe <= 0:
            return None
        mum_hacim = float(k[7])
        hacim24 = float(t["quoteVolume"])
        return {
            "sembol": sembol,
            "mum_acilis_ms": k[0],
            "acilis": acilis,
            "tepe": tepe,
            "giris": simdiki,
            "yukselis": (tepe - acilis) / acilis * 100,
            "anlik": (simdiki - acilis) / acilis * 100,
            "geri_cekilme": (tepe - simdiki) / tepe * 100,
            "mum_dk": (time.time() * 1000 - k[0]) / 60000,
            "mum_hacim_usd": mum_hacim,
            "alim_orani": float(k[10]) / mum_hacim * 100 if mum_hacim > 0 else 0,
            "islem_sayisi": int(k[8]),
            "hacim24_usd": hacim24,
            "hacim_payi": mum_hacim / hacim24 * 100 if hacim24 > 0 else 0,
            "degisim24": float(t["priceChangePercent"]),
        }
    except Exception:
        return None


def tara():
    r = requests.get(f"{BASE}/fapi/v1/ticker/24hr", timeout=15)
    tickers = r.json()
    if not isinstance(tickers, list):
        print("Binance futures hatası:", r.status_code, str(tickers)[:200])
        return

    btc24 = next((float(t["priceChangePercent"]) for t in tickers if t["symbol"] == "BTCUSDT"), 0.0)
    adaylar = [
        t for t in tickers
        if t["symbol"].endswith("USDT") and float(t["quoteVolume"]) >= MIN_HACIM_USD
    ]

    with ThreadPoolExecutor(max_workers=8) as ex:
        sonuclar = [m for m in ex.map(mum_kontrol, adaylar) if m]

    yeniler = []
    for m in sonuclar:
        anahtar = (m["sembol"], m["mum_acilis_ms"])
        if m["yukselis"] >= MIN_YUKSELIS and anahtar not in bildirilen:
            bildirilen.add(anahtar)
            m["btc24"] = btc24
            m["zaman_ms"] = int(time.time() * 1000)
            m["zaman"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            m["bitti"] = False
            sinyaller.append(m)
            yeniler.append(m)

    print(f"\n[{datetime.now().strftime('%H:%M:%S')}] {len(adaylar)} futures coin tarandı")
    if not yeniler:
        print("  Sinyal yok")
    else:
        satirlar = []
        for m in sorted(yeniler, key=lambda x: -x["yukselis"]):
            satirlar.append(
                f"🚀 {m['sembol']} (Futures)  tepe %{m['yukselis']:.2f} | anlık %{m['anlik']:.2f} | "
                f"geri çekilme %{m['geri_cekilme']:.2f}\n"
                f"Giriş: {m['giris']} | Mum hacmi: ${m['mum_hacim_usd']:,.0f} | "
                f"Alım oranı: %{m['alim_orani']:.0f} | İşlem: {m['islem_sayisi']}\n"
                f"24s hacim: ${m['hacim24_usd']:,.0f} | 24s değişim: %{m['degisim24']:.1f} | "
                f"BTC 24s: %{btc24:.1f} | Mumun {m['mum_dk']:.1f}. dk'sı"
            )
        yaz("\n\n".join(satirlar))
    kaydet()


# ---------- 24 SAATLİK DEĞERLENDİRME ----------
def degerlendir(s):
    try:
        r = requests.get(
            f"{BASE}/fapi/v1/klines",
            params={
                "symbol": s["sembol"],
                "interval": "15m",
                "startTime": s["mum_acilis_ms"] + 15 * 60 * 1000,
                "endTime": s["zaman_ms"] + DEGERLENDIRME_MS,
                "limit": 100,
            },
            timeout=10,
        )
        mumlar = r.json()
        if not isinstance(mumlar, list) or not mumlar:
            return None
        tepe = max(float(m[2]) for m in mumlar)
        dip = min(float(m[3]) for m in mumlar)
        son = float(mumlar[-1][4])
        g = s["giris"]
        degisim = (son - g) / g * 100
        return {
            "son_fiyat24": son,
            "degisim_24s": degisim,
            "max_yuk": (tepe - g) / g * 100,
            "max_dus": (dip - g) / g * 100,
            "sonuc": "YUKSELDI" if degisim > 0 else "DUSTU",
            "bitti": True,
        }
    except Exception:
        return None


def csv_yaz(s):
    yeni_dosya = not os.path.exists(CSV_DOSYA)
    with open(CSV_DOSYA, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=ALANLAR, extrasaction="ignore")
        if yeni_dosya:
            w.writeheader()
        w.writerow({k: (round(v, 4) if isinstance(v, float) else v) for k, v in s.items()})


def rapor_metni():
    biten = [s for s in sinyaller if s.get("bitti")]
    bekleyen = sum(1 for s in sinyaller if not s.get("bitti"))
    bugun = datetime.now().strftime("%Y-%m-%d")
    bugunku = sum(1 for s in sinyaller if s["zaman"].startswith(bugun))
    metin = f"📊 GÜN SONU RAPORU (Futures)\nBugün gelen sinyal: {bugunku}\nBekleyen (24s dolmadı): {bekleyen}\n"
    if not biten:
        return metin + "Henüz 24 saati dolan sinyal yok."
    yuk = [s for s in biten if s["sonuc"] == "YUKSELDI"]
    dus = [s for s in biten if s["sonuc"] == "DUSTU"]
    ort = lambda alan: sum(s[alan] for s in biten) / len(biten)
    return metin + (
        f"\n24 saatlik değerlendirme (toplam {len(biten)} sinyal)\n"
        f"Yükselen: {len(yuk)} | Düşen: {len(dus)} | Başarı: %{len(yuk) / len(biten) * 100:.0f}\n"
        f"Ort. 24s değişim: %{ort('degisim_24s'):.2f}\n"
        f"Ort. max yükseliş: %{ort('max_yuk'):.2f} | Ort. max düşüş: %{ort('max_dus'):.2f}"
    )


def bekleyenleri_degerlendir():
    simdi = int(time.time() * 1000)
    satirlar = []
    for s in sinyaller:
        if not s.get("bitti") and simdi - s["zaman_ms"] >= DEGERLENDIRME_MS:
            sonuc = degerlendir(s)
            if sonuc:
                s.update(sonuc)
                csv_yaz(s)
                isaret = "✅" if s["sonuc"] == "YUKSELDI" else "❌"
                satirlar.append(
                    f"{isaret} {s['sembol']} 24s sonuç: %{s['degisim_24s']:.2f} "
                    f"(max +%{s['max_yuk']:.1f} / max %{s['max_dus']:.1f})"
                )
    if satirlar:
        yaz("⏱ 24 SAAT SONUÇLARI\n" + "\n".join(satirlar))
        kaydet()
    bekleyen = sum(1 for s in sinyaller if not s.get("bitti"))
    print(f"  ⏳ 24 saati dolmayı bekleyen: {bekleyen} sinyal")


def gun_sonu_kontrol():
    simdi = datetime.now()
    if simdi.hour >= GUN_SONU_SAAT and durum["son_gun_sonu"] != simdi.date():
        durum["son_gun_sonu"] = simdi.date()
        yaz(rapor_metni())
        telegram_dosya(CSV_DOSYA)


# ---------- ANA DÖNGÜ ----------
yaz(f"✅ Futures tarama botu başladı\nŞart: 24s hacim ≥ ${MIN_HACIM_USD:,} ve 15dk mumda %{MIN_YUKSELIS:.0f}+ yükseliş\nTarama: {TARAMA_SANIYE // 60} dakikada bir")

while True:
    baslangic = time.time()
    try:
        tara()
        bekleyenleri_degerlendir()
        gun_sonu_kontrol()
    except Exception as e:
        print("Hata:", e)
    gecen = time.time() - baslangic
    time.sleep(max(1, TARAMA_SANIYE - gecen))
