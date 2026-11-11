import os, time, requests

TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT = os.getenv("TELEGRAM_CHAT_ID")
BASE = "https://fapi.binance.com"

MUM_YUZDE = 7          # 15dk mum boyu (yüksek-düşük) en az %
SMA_LEN = 100          # 4H SMA periyodu
OI_ESIK = 2.0          # GÜÇLÜ mesaj için mum süresince OI artışı en az %
TARAMA_ARALIK = 180    # tarama sıklığı (sn)
COOLDOWN = 1 * 3600    # aynı coinden tekrar mesaj için bekleme (1 saat)

oturum = requests.Session()
son_mesaj = {}         # normal mesaj: "SEMBOL" -> son mesaj zamanı
son_guclu = {}         # güçlü mesaj:  "SEMBOL" -> son mesaj zamanı
ilk_tarama = True


def get(path, **p):
    for _ in range(2):
        r = oturum.get(BASE + path, params=p, timeout=15)
        if r.status_code in (418, 429):
            time.sleep(int(r.headers.get("Retry-After", 120)))
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


def oi_degisim(s, mum_acilis_ms):
    """Mum açılışından bu yana Open Interest değişimi (%). Veri yoksa None."""
    h = get("/futures/data/openInterestHist", symbol=s, period="5m", limit=12)
    # Coin adedi kullanılır (USD değeri değil), böylece fiyat hareketi OI'yi şişirmez
    noktalar = [(int(x["timestamp"]), float(x["sumOpenInterest"])) for x in h]
    if len(noktalar) < 2:
        return None
    onceki = [n for n in noktalar if n[0] <= mum_acilis_ms]
    if not onceki:
        return None
    bas = onceki[-1][1]
    son = noktalar[-1][1]
    if bas <= 0:
        return None
    return (son - bas) / bas * 100


def tara():
    global ilk_tarama
    semboller = [t["symbol"] for t in get("/fapi/v1/ticker/24hr")
                 if t["symbol"].endswith("USDT") and "_" not in t["symbol"]]
    print(f"{len(semboller)} coin taranıyor")
    for s in semboller:
        try:
            simdi = time.time()
            normal_bekle = simdi - son_mesaj.get(s, 0) < COOLDOWN
            guclu_bekle = simdi - son_guclu.get(s, 0) < COOLDOWN
            if normal_bekle and guclu_bekle:
                continue

            k = get("/fapi/v1/klines", symbol=s, interval="15m", limit=3)[:-1]
            time.sleep(0.1)
            if not k:
                continue
            m = k[-1]                      # son kapanmış 15dk mum
            acilis_ms = int(m[0])
            acilis, yuksek, dusuk, kapanis = float(m[1]), float(m[2]), float(m[3]), float(m[4])
            boy = (yuksek - dusuk) / dusuk * 100
            if boy < MUM_YUZDE:
                continue

            h4 = get("/fapi/v1/klines", symbol=s, interval="4h", limit=SMA_LEN)
            time.sleep(0.1)
            if len(h4) < SMA_LEN:
                continue
            sma = sum(float(x[4]) for x in h4) / SMA_LEN
            if not (dusuk <= sma <= yuksek):
                continue

            yesil = kapanis >= acilis
            yon = "🟢 yeşil" if yesil else "🔴 kırmızı"

            # ── 1) NORMAL MESAJ (eski bot ile aynı) ──
            if not normal_bekle:
                son_mesaj[s] = time.time()
                if not ilk_tarama:         # bot yeni açıldı: eski mumu sessizce say
                    telegram(f"📍 4H SMA100 TEMASI {s}\n"
                             f"15dk mum: %{boy:.1f} ({yon})\n"
                             f"Mum: {dusuk} - {yuksek}\n"
                             f"4H SMA100: {sma:.6g}")

            # ── 2) GÜÇLÜ MESAJ (OI onaylı, ayrı) ──
            if not guclu_bekle:
                oi = oi_degisim(s, acilis_ms)
                time.sleep(0.1)
                if oi is not None and oi >= OI_ESIK:
                    son_guclu[s] = time.time()
                    if not ilk_tarama:
                        baslik = "🔥🟢 GÜÇLÜ LONG" if yesil else "🔥🔴 GÜÇLÜ SHORT"
                        telegram(f"{baslik} (OI ONAYLI) {s}\n"
                                 f"4H SMA100 teması + Open Interest artışı\n"
                                 f"15dk mum: %{boy:.1f} ({yon})\n"
                                 f"OI değişimi: +%{oi:.1f}\n"
                                 f"Mum: {dusuk} - {yuksek}\n"
                                 f"4H SMA100: {sma:.6g}")
        except Exception as e:
            print(s, e)
    ilk_tarama = False


while True:
    try:
        tara()
    except Exception as e:
        print("hata:", e)
    time.sleep(TARAMA_ARALIK)
