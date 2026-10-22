import time
import json
import csv
import os
import requests
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor

# ---- AYARLAR ----
MIN_HACIM_USD = 1_000_000   # 24 saatlik minimum hacim
MIN_YUKSELIS = 10.0         # açık 15dk mumda açılıştan tepeye minimum % artış
TARAMA_SANIYE = 7 * 60      # 7 dakikada bir tara
DEGERLENDIRME_MS = 24 * 3600 * 1000   # sinyalden 24 saat sonra değerlendir
JSON_DOSYA = "sinyaller.json"
CSV_DOSYA = "sonuclar.csv"
# -----------------------

BASE = "https://api.binance.com"

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


def kaydet():
    with open(JSON_DOSYA, "w", encoding="utf-8") as f:
        json.dump(sinyaller, f, ensure_ascii=False)


def mum_kontrol(t):
    sembol = t["symbol"]
    try:
        r = requests.get(
            f"{BASE}/api/v3/klines",
            params={"symbol": sembol, "interval": "15m", "limit": 1},
            timeout=10,
        )
        if r.status_code in (418, 429):
            time.sleep(10)
            return None
        k = r.json()[0]
        acilis = float(k[1])
        tepe = float(k[2])          # mumun en yüksek fiyatı
        simdiki = float(k[4])       # anlık fiyat
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
    tickers = requests.get(f"{BASE}/api/v3/ticker/24hr", timeout=15).json()
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

    print(f"\n[{datetime.now().strftime('%H:%M:%S')}] {len(adaylar)} coin tarandı")
    if not yeniler:
        print("  Sinyal yok")
    for m in sorted(yeniler, key=lambda x: -x["yukselis"]):
        print(
            f"  🚀 {m['sembol']}  tepe %{m['yukselis']:.2f}  anlık %{m['anlik']:.2f}  "
            f"geri çekilme %{m['geri_cekilme']:.2f}\n"
            f"     giriş {m['giris']}  | mum hacmi ${m['mum_hacim_usd']:,.0f}  "
            f"| alım oranı %{m['alim_orani']:.0f}  | işlem {m['islem_sayisi']}\n"
            f"     24s hacim ${m['hacim24_usd']:,.0f}  | 24s değişim %{m['degisim24']:.1f}  "
            f"| BTC 24s %{btc24:.1f}  | mumun {m['mum_dk']:.1f}. dk'sı"
        )
    kaydet()


def degerlendir(s):
    """Sinyalden sonraki 24 saati kontrol eder."""
    try:
        r = requests.get(
            f"{BASE}/api/v3/klines",
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


def rapor():
    biten = [s for s in sinyaller if s.get("bitti")]
    if not biten:
        return
    yuk = [s for s in biten if s["sonuc"] == "YUKSELDI"]
    dus = [s for s in biten if s["sonuc"] == "DUSTU"]
    ort = lambda alan: sum(s[alan] for s in biten) / len(biten)
    print("\n📊 24 SAATLİK DEĞERLENDİRME (toplam)")
    print(f"  Değerlendirilen: {len(biten)} sinyal")
    print(f"  Yükselen: {len(yuk)}  |  Düşen: {len(dus)}  |  Başarı: %{len(yuk) / len(biten) * 100:.0f}")
    print(f"  Ortalama 24s değişim: %{ort('degisim_24s'):.2f}")
    print(f"  Ortalama max yükseliş: %{ort('max_yuk'):.2f}  |  Ortalama max düşüş: %{ort('max_dus'):.2f}")


def bekleyenleri_degerlendir():
    simdi = int(time.time() * 1000)
    yeni = 0
    for s in sinyaller:
        if not s.get("bitti") and simdi - s["zaman_ms"] >= DEGERLENDIRME_MS:
            sonuc = degerlendir(s)
            if sonuc:
                s.update(sonuc)
                csv_yaz(s)
                yeni += 1
                isaret = "✅" if s["sonuc"] == "YUKSELDI" else "❌"
                print(f"  {isaret} {s['sembol']} 24s sonuç: %{s['degisim_24s']:.2f} "
                      f"(max +%{s['max_yuk']:.1f} / max %{s['max_dus']:.1f})")
    bekleyen = sum(1 for s in sinyaller if not s.get("bitti"))
    print(f"  ⏳ 24 saati dolmayı bekleyen: {bekleyen} sinyal")
    if yeni:
        rapor()
        kaydet()


rapor()
while True:
    baslangic = time.time()
    try:
        tara()
        bekleyenleri_degerlendir()
    except Exception as e:
        print("Hata:", e)
    gecen = time.time() - baslangic
    time.sleep(max(1, TARAMA_SANIYE - gecen))
