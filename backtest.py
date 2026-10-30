import time, csv, requests
from statistics import mean
from datetime import datetime, timezone

BASE = "https://fapi.binance.com"

GUN = 30                          # kaç günlük geçmiş test edilsin
TOP_N = 150                       # hacme göre ilk kaç coin
MUM_YUZDE = 5                     # itme mumu: open->high en az %
DELTA_ESIKLER = [3, 5, 10]        # itme deltası, önceki 20 mumun ortalama |delta|'sının kaç katı
GERI_CEKILME = 5.0                # tepeden %5 düşünce giriş
HEDEF = 10.0                      # girişten %10 kârda kapat
STOP_LISTESI = [3, 5, 10]         # stop % (girişten aşağı), hepsi ayrı test edilir
BEKLE_MUM = 4                    # geri çekilme için max bekleme (16 mum = 4 saat)
TUT_MUM = 96                      # işlemde max kalma (96 mum = 24 saat)
UCRET = 0.1                       # gidiş-dönüş komisyon %
ORT_MUM = 20

oturum = requests.Session()

def get(path, **p):
    for _ in range(2):
        r = oturum.get(BASE + path, params=p, timeout=15)
        if r.status_code in (418, 429):
            time.sleep(int(r.headers.get("Retry-After", 60)))
            continue
        r.raise_for_status()
        return r.json()
    raise RuntimeError("Rate limit devam ediyor")

def mumlar(s):
    bitis = int(time.time() * 1000)
    hedef = bitis - GUN * 86400 * 1000
    out = []
    while bitis > hedef:
        k = get("/fapi/v1/klines", symbol=s, interval="15m", limit=1500, endTime=bitis)
        time.sleep(0.5)
        if not k:
            break
        out = k + out
        bitis = k[0][0] - 1
        if len(k) < 1500:
            break
    return out[:-1]   # son (kapanmamış) mumu at

def test(s, k, delta_x, stop_pct):
    o = [float(x[1]) for x in k]
    h = [float(x[2]) for x in k]
    l = [float(x[3]) for x in k]
    c = [float(x[4]) for x in k]
    d = [2 * float(x[10]) - float(x[7]) for x in k]   # delta = 2*taker alış - toplam (USDT)
    sonuc = []
    i = ORT_MUM
    while i < len(k) - 1:
        ort = mean(abs(x) for x in d[i - ORT_MUM:i])
        mum = (h[i] - o[i]) / o[i] * 100
        if c[i] > o[i] and mum >= MUM_YUZDE and ort > 0 and d[i] >= ort * delta_x:
            tepe = h[i]
            giris = None
            for j in range(i + 1, min(i + 1 + BEKLE_MUM, len(k))):
                seviye = tepe * (1 - GERI_CEKILME / 100)
                if l[j] <= seviye:
                    giris = j
                    break
                tepe = max(tepe, h[j])          # tepe yükselmeye devam ederse güncelle
            if giris is None:
                i += 1
                continue
            hedef = seviye * (1 + HEDEF / 100)
            stop = seviye * (1 - stop_pct / 100)
            cikis, tip, cj = None, "SURE", min(giris + TUT_MUM, len(k)) - 1
            for j in range(giris, min(giris + TUT_MUM, len(k))):
                if l[j] <= stop:                       # önce stop kontrolü (kötümser)
                    cikis, tip, cj = stop, "STOP", j
                    break
                if j > giris and h[j] >= hedef:
                    cikis, tip, cj = hedef, "HEDEF", j
                    break
            if cikis is None:
                cikis = c[cj]
            getiri = (cikis - seviye) / seviye * 100 - UCRET
            sonuc.append({
                "sembol": s,
                "tarih": datetime.fromtimestamp(k[i][0] / 1000, timezone.utc).strftime("%Y-%m-%d %H:%M"),
                "delta_x": round(d[i] / ort, 1), "esik": delta_x, "stop": stop_pct,
                "itme_yuzde": round(mum, 1), "tepe": tepe, "giris": seviye, "cikis": cikis,
                "sonuc": tip, "getiri": round(getiri, 2)})
            i = cj + 1
        else:
            i += 1
    return sonuc

if __name__ == "__main__":
    ts = [t for t in get("/fapi/v1/ticker/24hr")
          if t["symbol"].endswith("USDT") and "_" not in t["symbol"]]
    ts.sort(key=lambda t: float(t["quoteVolume"]), reverse=True)
    semboller = [t["symbol"] for t in ts[:TOP_N]]

    veri = {}
    for n, s in enumerate(semboller, 1):
        try:
            veri[s] = mumlar(s)
            print(f"{n}/{len(semboller)} {s}")
        except Exception as e:
            print(s, e)

    tum = []
    print(f"\n{'delta':>6} {'stop%':>6} {'islem':>6} {'hedef%':>7} {'stop%':>6} {'basabas%':>9} {'ort.get%':>9} {'toplam%':>9}")
    for dx in DELTA_ESIKLER:
        for sp in STOP_LISTESI:
            rows = []
            for s, k in veri.items():
                rows += test(s, k, dx, sp)
            tum += rows
            n = len(rows)
            if n == 0:
                print(f"{dx:>5}x {sp:>6} {0:>6}")
                continue
            hedef_o = sum(1 for x in rows if x["sonuc"] == "HEDEF") / n * 100
            stop_o = sum(1 for x in rows if x["sonuc"] == "STOP") / n * 100
            basabas = (sp + UCRET) / (HEDEF + sp) * 100
            ort = sum(x["getiri"] for x in rows) / n
            top = sum(x["getiri"] for x in rows)
            print(f"{dx:>5}x {sp:>6} {n:>6} {hedef_o:>7.0f} {stop_o:>6.0f} {basabas:>9.0f} {ort:>9.2f} {top:>9.1f}")

    if tum:
        with open("backtest_islemler.csv", "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(tum[0].keys()))
            w.writeheader()
            w.writerows(tum)
        print("\nTüm işlemler: backtest_islemler.csv")
