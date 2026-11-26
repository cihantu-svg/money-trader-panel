import time, csv, statistics, requests
from datetime import datetime, timezone

BASE = "https://fapi.binance.com"

# ---------------- AYARLAR ----------------
GUN = 30             # kaç günlük geçmiş (15m mum)
MIN_HACIM = 1_000_000  # 24s USDT hacim alt sınırı (ana kodla aynı)
MAX_COIN = 300       # en fazla kaç coin taransın (hacme göre sıralı)
FEE = 0.08           # gidiş-dönüş komisyon % (taker 0.04 x 2)

# Kırılım (ana kodla AYNI)
MUM_YUZDE = 5
BO_LEN = 20
BO_TAMPON = 0.5
HACIM_CARPAN = 2.0
RSI_AL = 60
COOLDOWN_BAR = 4     # 1 saat = 4 mum

# Delta filtreleri
DELTA_BOY_X = 3.0    # F1: kırılım deltası, son 20 mumun ort. |delta|'sının kaç katı
ALICI_ORAN = 0.3     # F2: delta / hacim (0.3 ≈ alıcı payı %65)
YUTMA_LEN = 10       # F3: önceki kaç mumun satış deltası yutulsun
SERT_KIRMIZI = 0.5   # F4: sonraki mum deltası, kırılım deltasının -%50'sinden kötüyse ele

# Çıkış kuralları
MAX_BAR = 96         # en fazla 24 saat tut
CIKIS_TEK = 0.5      # tek mum deltası <= -%50 kırılım deltası -> çık
CIKIS_BIRIKIM = 1.0  # birikimli satış deltası <= -%100 kırılım deltası -> çık

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
    return out[:-1]  # son (kapanmamış) mumu at


def isle(s, k, islemler):
    n = len(k)
    if n < 200:
        return
    o = [float(x[1]) for x in k]
    h = [float(x[2]) for x in k]
    l = [float(x[3]) for x in k]
    c = [float(x[4]) for x in k]
    v = [float(x[5]) for x in k]
    qv = [float(x[7]) for x in k]
    delta = [2 * float(x[10]) - float(x[7]) for x in k]  # alıcı - satıcı (USDT)

    macd = [a - b for a, b in zip(ema(c, 12), ema(c, 26))]
    hist = [m - sg for m, sg in zip(macd, ema(macd, 9))]
    rsi = rsi_seri(c)

    son_i = -999
    for i in range(60, n - 2 - MAX_BAR):
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
        if D <= 0:
            D_ok = False
        else:
            D_ok = True

        ort_abs = sum(abs(x) for x in delta[i - 20:i]) / 20 or 1e-9
        f_boy = D_ok and D >= DELTA_BOY_X * ort_abs
        f_alici = D_ok and qv[i] > 0 and D / qv[i] >= ALICI_ORAN
        satis = sum(-x for x in delta[i - YUTMA_LEN:i] if x < 0)
        f_yutma = D_ok and D > satis
        f_tut = D_ok and delta[i + 1] > -SERT_KIRMIZI * D

        # strateji adı -> (filtre geçti mi, giriş mumu)
        stratejiler = {
            "0_TEMEL (sadece kirilim)": (True, i),
            "0_TEMEL giris i+1": (True, i + 1),
            "1_DELTA_BOY": (f_boy, i),
            "2_ALICI_PAYI": (f_alici, i),
            "3_YUTMA": (f_yutma, i),
            "4_TUTUNMA (i+1 giris)": (f_tut, i + 1),
            "5_KOMBINE (1+2+3)": (f_boy and f_alici and f_yutma, i),
        }
        stop = l[i]
        tarih = datetime.fromtimestamp(k[i][0] / 1000, timezone.utc).strftime("%Y-%m-%d %H:%M")
        for ad, (gecti, e) in stratejiler.items():
            if not gecti:
                continue
            for cikis in ("STOP+SABIT", "STOP+DELTA"):
                getiri, neden = sim(e, c, o, l, delta, stop, D, cikis == "STOP+DELTA")
                islemler.append({"sembol": s, "tarih": tarih, "strateji": ad,
                                 "cikis": cikis, "neden": neden, "getiri": getiri})


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


def rapor(islemler):
    print(f"\n{'STRATEJI':28} {'CIKIS':11} {'ISLEM':>5} {'KAZAN%':>7} {'ORT%':>7} {'MEDYAN%':>8} {'TOPLAM%':>8}")
    print("-" * 80)
    adlar = sorted({r["strateji"] for r in islemler})
    for ad in adlar:
        for cikis in ("STOP+SABIT", "STOP+DELTA"):
            g = [r["getiri"] for r in islemler if r["strateji"] == ad and r["cikis"] == cikis]
            if not g:
                continue
            kazan = sum(1 for x in g if x > 0) / len(g) * 100
            print(f"{ad:28} {cikis:11} {len(g):5d} {kazan:7.0f} {statistics.mean(g):7.2f} "
                  f"{statistics.median(g):8.2f} {sum(g):8.1f}")
        print()


def main():
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
            isle(s, mumlar(s), islemler)
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
    rapor(islemler)
    print("Tum islemler: backtest_islemler.csv")


if __name__ == "__main__":
    main()
