import time, statistics, requests

BASE = "https://fapi.binance.com"

# ---------------- AYARLAR ----------------
GUN = 60               # kac gunluk 15m gecmis
MIN_HACIM = 1_000_000  # 24s USDT hacim alt siniri
MAX_COIN = 300
VX_MIN = 4.0           # "bir sey oldu" esigi: hacim, onceki 20 mum ortalamasinin kac kati
UFUK_KISA = 4          # 4 mum = 1 saat sonrasi
UFUK_UZUN = 16         # 16 mum = 4 saat sonrasi

# delta/hacim gruplari: -1 (hepsi satici) ... +1 (hepsi alici)
GRUPLAR = [(-1.01, -0.3, "cok satici   (< -0.3)"),
           (-0.3, -0.1, "satici       (-0.3..-0.1)"),
           (-0.1, 0.1, "dengeli      (-0.1..0.1)"),
           (0.1, 0.3, "alici        (0.1..0.3)"),
           (0.3, 1.01, "cok alici    (> 0.3)")]

oturum = requests.Session()


def get(path, **p):
    for _ in range(3):
        r = oturum.get(BASE + path, params=p, timeout=15)
        if r.status_code in (418, 429):
            bekle = int(r.headers.get("Retry-After", 60))
            print(f"Rate limit, {bekle} sn bekleniyor")
            time.sleep(bekle)
            continue
        r.raise_for_status()
        return r.json()
    raise RuntimeError("Rate limit devam ediyor")


def mumlar(s):
    simdi = int(time.time() * 1000)
    bas = simdi - GUN * 86400000
    out = []
    while bas < simdi:
        k = get("/fapi/v1/klines", symbol=s, interval="15m", startTime=bas, limit=1500)
        if not k:
            break
        out += k
        bas = k[-1][0] + 900000
        if len(k) < 1500:
            break
        time.sleep(0.4)
    return out[:-1]


def olaylar_cikar(k, olaylar, baz):
    n = len(k)
    if n < 100:
        return
    o = [float(x[1]) for x in k]
    c = [float(x[4]) for x in k]
    v = [float(x[5]) for x in k]
    qv = [float(x[7]) for x in k]
    delta = [2 * float(x[10]) - float(x[7]) for x in k]
    for i in range(20, n - UFUK_UZUN - 1):
        ort = sum(v[i - 20:i]) / 20
        if ort <= 0 or qv[i] <= 0:
            continue
        vx = v[i] / ort
        f1 = (c[i + UFUK_KISA] / c[i] - 1) * 100
        f4 = (c[i + UFUK_UZUN] / c[i] - 1) * 100
        if vx < 2:
            baz.append(f4)           # sakin mumlar: piyasanin kendi egilimi
        if vx >= VX_MIN:
            olaylar.append({"dr": delta[i] / qv[i], "yesil": c[i] >= o[i], "f1": f1, "f4": f4})


def satir(ad, grup):
    if len(grup) < 5:
        print(f"{ad:30} {len(grup):6d}   (cok az)")
        return
    f1 = [x["f1"] for x in grup]
    f4 = [x["f4"] for x in grup]
    sd = statistics.pstdev(f4)
    t = statistics.mean(f4) / (sd / len(f4) ** 0.5) if sd > 0 else 0
    yuk = sum(1 for x in f4 if x > 0) / len(f4) * 100
    print(f"{ad:30} {len(grup):6d} {statistics.mean(f1):8.2f} {statistics.mean(f4):8.2f} "
          f"{statistics.median(f4):8.2f} {yuk:7.0f} {t:6.1f}")


def rapor(olaylar, baz):
    print(f"\nHacim >= {VX_MIN:.0f}x olan {len(olaylar)} mum. Sonraki 1 saat ve 4 saatlik getiri (komisyon dusulmedi).")
    print(f"Karsilastirma: sakin mumlarin (hacim < 2x) 4s ort. getirisi %{statistics.mean(baz):.2f}, "
          f"medyan %{statistics.median(baz):.2f}")
    print(f"\n{'DELTA / HACIM':30} {'ISLEM':>6} {'1s ort%':>8} {'4s ort%':>8} {'4s med%':>8} {'4s yuk%':>7} {'t':>6}")
    print("-" * 80)
    for yesil, etiket in ((True, "YESIL mum (fiyat yukari)"), (False, "KIRMIZI mum (fiyat asagi)")):
        print(f"\n{etiket}")
        for lo, hi, ad in GRUPLAR:
            grup = [x for x in olaylar if x["yesil"] == yesil and lo <= x["dr"] < hi]
            satir("  " + ad, grup)
    print("\n't' degeri: |t| < 2 ise o satirdaki ortalama sansla aciklanabilir, guvenme.")


def main():
    tickerlar = [t for t in get("/fapi/v1/ticker/24hr")
                 if t["symbol"].endswith("USDT") and "_" not in t["symbol"]
                 and float(t["quoteVolume"]) >= MIN_HACIM]
    tickerlar.sort(key=lambda t: float(t["quoteVolume"]), reverse=True)
    tickerlar = tickerlar[:MAX_COIN]
    print(f"{len(tickerlar)} coin, {GUN} gun taranacak")
    olaylar, baz = [], []
    for n, t in enumerate(tickerlar, 1):
        try:
            olaylar_cikar(mumlar(t["symbol"]), olaylar, baz)
        except Exception as ex:
            print(t["symbol"], ex)
        if n % 25 == 0:
            print(f"{n}/{len(tickerlar)} bitti")
        time.sleep(0.3)
    if len(olaylar) < 50:
        print("Cok az olay. VX_MIN'i dusur ya da GUN'u artir.")
        return
    rapor(olaylar, baz)


if __name__ == "__main__":
    main()
