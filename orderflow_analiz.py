import time, statistics, requests

BASE = "https://fapi.binance.com"

# ---------------- AYARLAR ----------------
GUN = 60
MIN_HACIM = 1_000_000
MAX_COIN = 300
FEE = 0.08               # gidis-donus komisyon %

# Bizim kirilim (Pine kodundaki varsayilanlar)
BO_LEN = 20
BO_TAMPON = 0.5
HACIM_CARPAN = 2.0       # Pine'da varsayilan 2.0
RSI_AL = 60
COOLDOWN_BAR = 5

# SMA100 (15m) filtresi
SMA_LEN = 100
SMA_ESIK = 5.0           # kapanis SMA100'e en fazla %5 uzakta ya da mum SMA100'e temas etsin

# Flux Charts "Breakout Volume Delta" gostergesi (varsayilanlar)
SW_L = 10                # swing sol
SW_R = 10                # swing sag
MAX_SEVIYE = 5           # grafikte tutulan en yakin seviye sayisi
FLUX_PENCERE = 3         # bizim kirilimin 3 mum solunda veya sagnda Flux kirilimi
FLUX_ALICI_MIN = 0.2     # F4 stratejisi: delta/hacim >= 0.2 (yaklasik %60 alici payi)

# Cikis
TP_LISTE = [1.0, 2.0, 3.0]   # hedef kar % (ayni tabloda hepsi denenir)
SABIT_STOP = 2.0             # S2: sabit stop %
MAX_BAR = 96                 # en fazla 24 saat

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
    return out[:-1]


def flux_kirilim(h, c):
    """Flux 'Breakout Volume Delta': swing tepesi (10 sol/10 sag) kapanisla yukari kirildi mi.
    Seviye, tepeden 11 mum sonra aktif olur (ileriye bakma yok). Son 5 seviye tutulur."""
    n = len(c)
    seviyeler = []   # [fiyat, kirildi_mi]
    olay = [False] * n
    for x in range(2 * SW_L + 1, n):
        p = x - SW_R - 1
        if h[p] > max(h[p - SW_L:p]) and h[p] > max(h[p + 1:p + SW_R + 1]):
            seviyeler.append([h[p], False])
            if len(seviyeler) > MAX_SEVIYE:
                seviyeler.pop(0)
        for sv in seviyeler:
            if not sv[1] and c[x] > sv[0]:
                sv[1] = True
                olay[x] = True
    return olay


def sim(e, c, o, h, l, stop, tp):
    giris = c[e]
    hedef = giris * (1 + tp / 100)
    for j in range(e + 1, e + 1 + MAX_BAR):
        if l[j] <= stop:                              # ayni mumda ikisi de olursa stop sayilir
            return (min(stop, o[j]) / giris - 1) * 100 - FEE, "stop"
        if h[j] >= hedef:
            return tp - FEE, "hedef"
    return (c[e + MAX_BAR] / giris - 1) * 100 - FEE, "sure"


def isle(s, k, sonuc):
    n = len(k)
    if n < 300:
        return
    o = [float(x[1]) for x in k]
    h = [float(x[2]) for x in k]
    l = [float(x[3]) for x in k]
    c = [float(x[4]) for x in k]
    v = [float(x[5]) for x in k]
    qv = [float(x[7]) for x in k]
    delta = [2 * float(x[10]) - float(x[7]) for x in k]

    macd = [a - b for a, b in zip(ema(c, 12), ema(c, 26))]
    hist = [m - sg for m, sg in zip(macd, ema(macd, 9))]
    rsi = rsi_seri(c)
    flux = flux_kirilim(h, c)
    pref = [0.0]
    for x in c:
        pref.append(pref[-1] + x)

    son_i = -999
    for i in range(SMA_LEN, n - 4 - MAX_BAR):
        if i - son_i < COOLDOWN_BAR:
            continue
        direnc = max(h[i - BO_LEN:i])
        kirilim = c[i] > direnc * (1 + BO_TAMPON / 100) and c[i - 1] <= direnc
        hacim_ok = v[i] > HACIM_CARPAN * (sum(v[i - 19:i + 1]) / 20)
        mom = rsi[i] > RSI_AL and hist[i] > 0
        if not (kirilim and hacim_ok and mom):
            continue
        son_i = i

        sma = (pref[i + 1] - pref[i + 1 - SMA_LEN]) / SMA_LEN
        sma_ok = abs((c[i] - sma) / sma * 100) <= SMA_ESIK or l[i] <= sma <= h[i]

        # Flux kirilimi: i-3 .. i+3 icinde; giris, ikisinden DAHA GEC olanin kapanisinda
        def flux_giris(sart):
            adaylar = [f for f in range(i - FLUX_PENCERE, i + FLUX_PENCERE + 1)
                       if flux[f] and sart(f)]
            return min((max(i, f) for f in adaylar), default=None)

        e_flux = flux_giris(lambda f: True)
        e_flux_delta = flux_giris(lambda f: qv[f] > 0 and delta[f] / qv[f] >= FLUX_ALICI_MIN)

        tarih_ok = []
        girisler = {
            "0_PINE kirilim": i,
            "1_PINE + SMA100": i if sma_ok else None,
            "2_PINE + FLUX +-3": e_flux,
            "3_PINE + FLUX + SMA": e_flux if sma_ok else None,
            "4_PINE + FLUX(alici>=60%)": e_flux_delta,
        }
        for ad, e in girisler.items():
            if e is None:
                continue
            giris = c[e]
            for stop_tipi in ("S1 kirilim dibi", "S2 sabit %2"):
                stop = l[i] if stop_tipi.startswith("S1") else giris * (1 - SABIT_STOP / 100)
                if stop >= giris:
                    continue
                for tp in TP_LISTE:
                    getiri, neden = sim(e, c, o, h, l, stop, tp)
                    sonuc.append((ad, stop_tipi, tp, getiri, neden))


def rapor(sonuc):
    for stop_tipi in ("S1 kirilim dibi", "S2 sabit %2"):
        print(f"\n===== STOP: {stop_tipi} =====")
        print(f"{'STRATEJI':27} {'HEDEF%':>6} {'ISLEM':>6} {'HEDEFE%':>8} {'STOPA%':>7} {'SURE%':>6} {'ORT%':>7}")
        print("-" * 72)
        for ad in sorted({r[0] for r in sonuc}):
            for tp in TP_LISTE:
                g = [r for r in sonuc if r[0] == ad and r[1] == stop_tipi and r[2] == tp]
                if not g:
                    continue
                m = len(g)
                hedef = sum(1 for r in g if r[4] == "hedef") / m * 100
                stp = sum(1 for r in g if r[4] == "stop") / m * 100
                sure = sum(1 for r in g if r[4] == "sure") / m * 100
                ort = statistics.mean(r[3] for r in g)
                print(f"{ad:27} {tp:6.0f} {m:6d} {hedef:8.0f} {stp:7.0f} {sure:6.0f} {ort:7.2f}")
            print()
    print("ORT% = komisyon dusulmus ortalama islem getirisi. Isaret onemli: eksi ise o ayarla kaybedersin.")


def main():
    tickerlar = [t for t in get("/fapi/v1/ticker/24hr")
                 if t["symbol"].endswith("USDT") and "_" not in t["symbol"]
                 and float(t["quoteVolume"]) >= MIN_HACIM]
    tickerlar.sort(key=lambda t: float(t["quoteVolume"]), reverse=True)
    tickerlar = tickerlar[:MAX_COIN]
    print(f"{len(tickerlar)} coin, {GUN} gun taranacak")
    sonuc = []
    for n, t in enumerate(tickerlar, 1):
        try:
            isle(t["symbol"], mumlar(t["symbol"]), sonuc)
        except Exception as ex:
            print(t["symbol"], ex)
        if n % 25 == 0:
            print(f"{n}/{len(tickerlar)} bitti")
        time.sleep(0.3)
    if not sonuc:
        print("Hic sinyal cikmadi.")
        return
    rapor(sonuc)


if __name__ == "__main__":
    main()
