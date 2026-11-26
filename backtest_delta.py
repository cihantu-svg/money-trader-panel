import sys, time, csv, statistics, bisect, requests
from datetime import datetime, timezone

BASE = "https://fapi.binance.com"

# ---------------- AYARLAR ----------------
GUN = 30               # kac gunluk gecmis (15m mum)
MIN_HACIM = 1_000_000  # 24s USDT hacim alt siniri (ana kodla ayni)
MAX_COIN = 300         # en fazla kac coin (hacme gore sirali)
FEE = 0.08             # gidis-donus komisyon %

# Kirilim (ana kodla AYNI)
MUM_YUZDE = 5
BO_LEN = 20
BO_TAMPON = 0.5
HACIM_CARPAN = 2.0
RSI_AL = 60
COOLDOWN_BAR = 4       # 1 saat = 4 mum

# Yeni filtreler
UC_MUM = 3             # kirilimdan sonra kac mumun toplam deltasi (giris bu mumun kapanisinda)
SMA_LEN = 100
SMA_ESIK = 2.0         # fiyat, SMA100'e en fazla % kac uzakta olsun

# Cikis
MAX_BAR = 96           # en fazla 24 saat tut
CIKIS_TEK = 0.5        # tek mum deltasi <= -%50 kirilim deltasi -> cik
CIKIS_BIRIKIM = 1.0    # birikimli satis deltasi <= -%100 kirilim deltasi -> cik

oturum = requests.Session()
METRIKLER = []


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


def ema(vals, n):
    k = 2 / (n + 1)
    out = [vals[0]]
    for x in vals[1:]:
        out.append(x * k + out[-1] * (1 - k))
    return out


def rsi_seri(c, n=14):
    out = [50.0] * len(c)
    if len(c) <= n:
        return out
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
    return out[:-1]  # kapanmamis son mumu at


def mumlar_4h(s):
    simdi = int(time.time() * 1000)
    return get("/fapi/v1/klines", symbol=s, interval="4h",
               startTime=simdi - (GUN + 20) * 86400000, limit=1500)


def sim(e, c, o, l, delta, stop, D, delta_cikis):
    giris = c[e]
    birikim = 0.0
    for j in range(e + 1, e + 1 + MAX_BAR):
        if l[j] < stop:
            fiyat = min(stop, o[j])
            return (fiyat / giris - 1) * 100 - FEE, "stop"
        if delta_cikis:
            if delta[j] <= -CIKIS_TEK * D:
                return (c[j] / giris - 1) * 100 - FEE, "sert_kirmizi"
            birikim += min(delta[j], 0)
            if birikim <= -CIKIS_BIRIKIM * D:
                return (c[j] / giris - 1) * 100 - FEE, "birikimli"
    return (c[e + MAX_BAR] / giris - 1) * 100 - FEE, "sure"


def isle(s, k, k4, islemler, detay=False):
    n = len(k)
    if n < 250:
        return
    o = [float(x[1]) for x in k]
    h = [float(x[2]) for x in k]
    l = [float(x[3]) for x in k]
    c = [float(x[4]) for x in k]
    v = [float(x[5]) for x in k]
    qv = [float(x[7]) for x in k]
    delta = [2 * float(x[10]) - float(x[7]) for x in k]  # alici - satici (USDT)

    macd = [a - b for a, b in zip(ema(c, 12), ema(c, 26))]
    hist = [m - sg for m, sg in zip(macd, ema(macd, 9))]
    rsi = rsi_seri(c)

    pref = [0.0]
    for x in c:
        pref.append(pref[-1] + x)

    # 4h SMA100: sadece KAPANMIS 4h mumlar kullanilir (ileriye bakma yok)
    c4 = [float(x[4]) for x in k4]
    kap4, sma4 = [], []
    for m in range(SMA_LEN - 1, len(k4)):
        kap4.append(k4[m][0] + 14400000)
        sma4.append(sum(c4[m - SMA_LEN + 1:m + 1]) / SMA_LEN)

    son_i = -999
    for i in range(SMA_LEN, n - UC_MUM - 1 - MAX_BAR):
        if i - son_i < COOLDOWN_BAR:
            continue
        direnc = max(h[i - BO_LEN:i])
        kirilim = c[i] > direnc * (1 + BO_TAMPON / 100) and c[i - 1] <= direnc
        hacim_ok = v[i] > HACIM_CARPAN * (sum(v[i - 19:i + 1]) / 20)
        mom = rsi[i] > RSI_AL and hist[i] > 0
        mum = (h[i] - o[i]) / o[i] * 100 >= MUM_YUZDE
        if not (kirilim and hacim_ok and mom and mum):
            continue
        son_i = i

        D = delta[i]
        yon = abs(D) or 1e-9
        ort_abs = sum(abs(x) for x in delta[i - 20:i]) / 20 or 1e-9
        satis = sum(-x for x in delta[i - 10:i] if x < 0)
        uc = sum(delta[i + 1:i + 1 + UC_MUM])  # kirilimdan sonraki 3 mumun toplam deltasi

        sma15 = (pref[i + 1] - pref[i + 1 - SMA_LEN]) / SMA_LEN
        m15 = (c[i] - sma15) / sma15 * 100
        idx = bisect.bisect_right(kap4, k[i][0] + 900000) - 1
        m4 = (c[i] - sma4[idx]) / sma4[idx] * 100 if idx >= 0 else None

        f3 = uc > 0
        f15 = abs(m15) <= SMA_ESIK
        f4 = m4 is not None and abs(m4) <= SMA_ESIK
        tarih = datetime.fromtimestamp(k[i][0] / 1000, timezone.utc).strftime("%Y-%m-%d %H:%M")

        if detay:
            ev = lambda b: "EVET" if b else "hayir"
            m4t = f"{m4:+.1f}%" if m4 is not None else "yok"
            print(f"{s} {tarih} UTC | fiyat {c[i]} | mum %{(h[i]-o[i])/o[i]*100:.1f} | "
                  f"delta {D/1e3:.0f}K | 3mum toplam {uc/1e3:.0f}K | "
                  f"SMA15m {m15:+.1f}% SMA4h {m4t} | "
                  f"3MUM:{ev(f3)} SMA15m:{ev(f15)} SMA4h:{ev(f4)}")
            continue

        stop = l[i]
        e3 = i + UC_MUM
        METRIKLER.append({
            "sembol": s, "tarih": tarih,
            "delta_x": D / ort_abs,
            "alici": D / qv[i] if qv[i] else 0,
            "yutma": max(-50, min(50, D / (satis + 1e-9))),
            "uc_mum": uc / yon,
            "sma15_mesafe": m15,
            "sma4h_mesafe": m4,
            "getiri_sabit": sim(e3, c, o, l, delta, stop, yon, False)[0],
            "getiri_delta": sim(e3, c, o, l, delta, stop, yon, True)[0]})

        stratejiler = {
            "0_TEMEL giris i": (True, i),
            "0_TEMEL giris i+3": (True, e3),
            "A_3MUM_TOPLAM>0": (f3, e3),
            "B_SMA15m<=2%": (f15, i),
            "C_SMA4h<=2%": (f4, i),
            "D_3MUM + SMA15m": (f3 and f15, e3),
            "E_3MUM + SMA4h": (f3 and f4, e3),
        }
        for ad, (gecti, e) in stratejiler.items():
            if not gecti:
                continue
            for cikis in ("STOP+SABIT", "STOP+DELTA"):
                getiri, neden = sim(e, c, o, l, delta, stop, yon, cikis == "STOP+DELTA")
                islemler.append({"sembol": s, "tarih": tarih, "strateji": ad,
                                 "cikis": cikis, "neden": neden, "getiri": getiri})


def rapor(islemler):
    print(f"\n{'STRATEJI':22} {'CIKIS':11} {'ISLEM':>5} {'KAZAN%':>7} {'ORT%':>7} {'MEDYAN%':>8}")
    print("-" * 66)
    for ad in sorted({r["strateji"] for r in islemler}):
        for cikis in ("STOP+SABIT", "STOP+DELTA"):
            g = [r["getiri"] for r in islemler if r["strateji"] == ad and r["cikis"] == cikis]
            if not g:
                continue
            kazan = sum(1 for x in g if x > 0) / len(g) * 100
            print(f"{ad:22} {cikis:11} {len(g):5d} {kazan:7.0f} {statistics.mean(g):7.2f} "
                  f"{statistics.median(g):8.2f}")
        print()


def metrik_rapor():
    print("\n=== HER OLCU ISE YARIYOR MU? (filtresiz kirilimlar 4 esit gruba bolundu) ===")
    print("Giris: kirilimdan 3 mum sonra. Olcu gercekten bilgi veriyorsa sonuc gruptan gruba duzenli degismeli.")
    olcumler = [("delta_x", "1) Kirilim deltasi / son 20 mum ort |delta|"),
                ("alici", "2) Delta / hacim (alici baskinligi)"),
                ("yutma", "3) Delta / onceki 10 mum satis deltasi"),
                ("uc_mum", "4) Sonraki 3 mumun toplam deltasi / kirilim deltasi"),
                ("sma15_mesafe", "5) Fiyatin 15m SMA100'e uzakligi % (+ ustunde, - altinda)"),
                ("sma4h_mesafe", "6) Fiyatin 4h SMA100'e uzakligi %")]
    for anahtar, baslik in olcumler:
        r = sorted((x for x in METRIKLER if x[anahtar] is not None), key=lambda x: x[anahtar])
        if len(r) < 8:
            continue
        print(f"\n{baslik}")
        print(f"{'GRUP (dusukten yuksege)':28} {'ISLEM':>5} | {'SABIT kazan%':>12} {'ort%':>6} | {'DELTA kazan%':>12} {'ort%':>6}")
        d = len(r) / 4
        for g in range(4):
            parca = r[int(g * d):int((g + 1) * d)]
            aralik = f"{parca[0][anahtar]:.2f} .. {parca[-1][anahtar]:.2f}"
            ks = sum(1 for x in parca if x["getiri_sabit"] > 0) / len(parca) * 100
            kd = sum(1 for x in parca if x["getiri_delta"] > 0) / len(parca) * 100
            os_ = statistics.mean(x["getiri_sabit"] for x in parca)
            od = statistics.mean(x["getiri_delta"] for x in parca)
            print(f"{aralik:28} {len(parca):5d} | {ks:12.0f} {os_:6.2f} | {kd:12.0f} {od:6.2f}")


def kontrol(semboller):
    """python backtest_delta.py MANTRAUSDT SLXUSDT -> o coinlerin sinyallerini tek tek yazar"""
    for s in semboller:
        print(f"\n--- {s} (son {GUN} gun, UTC saat) ---")
        isle(s, mumlar(s), mumlar_4h(s), [], detay=True)


def main():
    if len(sys.argv) > 1:
        kontrol([x.upper() for x in sys.argv[1:]])
        return
    tickerlar = [t for t in get("/fapi/v1/ticker/24hr")
                 if t["symbol"].endswith("USDT") and "_" not in t["symbol"]
                 and float(t["quoteVolume"]) >= MIN_HACIM]
    tickerlar.sort(key=lambda t: float(t["quoteVolume"]), reverse=True)
    tickerlar = tickerlar[:MAX_COIN]
    print(f"{len(tickerlar)} coin, {GUN} gun taranacak")
    islemler = []
    for n, t in enumerate(tickerlar, 1):
        s = t["symbol"]
        try:
            isle(s, mumlar(s), mumlar_4h(s), islemler)
        except Exception as ex:
            print(s, ex)
        if n % 25 == 0:
            print(f"{n}/{len(tickerlar)} bitti")
        time.sleep(0.3)
    if not islemler:
        print("Hic sinyal cikmadi. MUM_YUZDE'yi dusurup (ornek 3) tekrar dene.")
        return
    with open("backtest_islemler.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(islemler[0].keys()))
        w.writeheader()
        w.writerows(islemler)
    with open("backtest_metrikler.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(METRIKLER[0].keys()))
        w.writeheader()
        w.writerows(METRIKLER)
    rapor(islemler)
    metrik_rapor()
    print("\nDosyalar: backtest_islemler.csv, backtest_metrikler.csv")


if __name__ == "__main__":
    main()
