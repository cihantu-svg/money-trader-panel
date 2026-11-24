import os, time, csv, requests
from bisect import bisect_right
from datetime import datetime, timezone

TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT = os.getenv("TELEGRAM_CHAT_ID")
BASE = "https://fapi.binance.com"

# ---------- AYARLAR ----------
GUN = 30             # kaç gün geriye test
COIN_SAYISI = 150    # hacme göre en büyük kaç coin
MIN_HACIM = 1_000_000
SMA_LEN = 100        # 4H SMA periyodu
TOLERANS = 1.0       # 15dk mumun SMA'ya dokunma toleransı (%)
COOLDOWN_MUM = 8     # aynı coinde iki sinyal arası min mum (8 x 15dk = 2 saat)
TP = 5.0             # kazanç hedefi (%)
SL = 3.0             # zarar limiti (%)
IZLEME_MUM = 96      # sinyalden sonra kaç mum izlenir (96 x 15dk = 24 saat)
DOSYA = "sma_backtest.csv"

ALANLAR = ["sembol", "tarih", "yon", "fiyat", "sma", "mesafe_yuzde", "sma_egim",
           "mum_boy", "alt_fitil", "ust_fitil", "mum_renk", "hacim_x",
           "gelis_hareket", "sonuc", "dk", "max_lehine", "max_aleyhine"]

oturum = requests.Session()


def get(path, **p):
    for _ in range(3):
        r = oturum.get(BASE + path, params=p, timeout=20)
        if r.status_code in (418, 429):
            bekle = int(r.headers.get("Retry-After", 60))
            print(f"Rate limit, {bekle} sn bekleniyor")
            time.sleep(bekle)
            continue
        r.raise_for_status()
        return r.json()
    raise RuntimeError("Rate limit devam ediyor")


def telegram(msg):
    for i in range(0, len(msg), 3900):
        try:
            oturum.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                        data={"chat_id": CHAT, "text": msg[i:i + 3900]}, timeout=20)
        except Exception as e:
            print("telegram hata:", e)


def telegram_dosya():
    try:
        with open(DOSYA, "rb") as f:
            oturum.post(f"https://api.telegram.org/bot{TOKEN}/sendDocument",
                        data={"chat_id": CHAT}, files={"document": f}, timeout=60)
    except Exception as e:
        print("dosya hata:", e)


def mumlar_15m(s):
    bas = int((time.time() - GUN * 86400) * 1000)
    out = []
    while True:
        k = get("/fapi/v1/klines", symbol=s, interval="15m", startTime=bas, limit=1500)
        out += k
        if len(k) < 1500:
            break
        bas = k[-1][0] + 1
    return out[:-1]   # kapanmamış son mumu at


def mumlar_4h(s):
    bas = int((time.time() - (GUN * 6 + SMA_LEN + 10) * 4 * 3600) * 1000)
    return get("/fapi/v1/klines", symbol=s, interval="4h", startTime=bas, limit=1500)


def coin_test(s):
    k4 = mumlar_4h(s)
    time.sleep(0.3)
    k15 = mumlar_15m(s)
    if len(k4) < SMA_LEN + 10 or len(k15) < 150:
        return []

    t4 = [x[0] for x in k4]
    c4 = [float(x[4]) for x in k4]
    # sma[i] = i. 4H mumdan ÖNCEKİ 100 kapanmış mumun ortalaması (geleceği görmez)
    sma = {i: sum(c4[i - SMA_LEN:i]) / SMA_LEN for i in range(SMA_LEN, len(c4))}

    o = [float(x[1]) for x in k15]
    h = [float(x[2]) for x in k15]
    l = [float(x[3]) for x in k15]
    c = [float(x[4]) for x in k15]
    v = [float(x[5]) for x in k15]

    sonuclar, son_j = [], -999
    for j in range(20, len(k15) - IZLEME_MUM):
        i = bisect_right(t4, k15[j][0]) - 1
        if i - 6 < SMA_LEN:
            continue
        s_ = sma[i]
        tol = TOLERANS / 100
        if not (l[j] <= s_ * (1 + tol) and h[j] >= s_ * (1 - tol)):
            continue
        if j - son_j < COOLDOWN_MUM:
            continue
        son_j = j

        yon = "AL" if c[j - 1] > s_ else "SAT"   # SMA'ya yukarıdan geldiyse AL, aşağıdan geldiyse SAT
        e = c[j]
        rng = h[j] - l[j]
        vol_ort = sum(v[j - 20:j]) / 20

        maxl = maxa = 0.0
        sonuc, dk = "BELIRSIZ", ""
        for m in range(j + 1, j + 1 + IZLEME_MUM):
            if yon == "AL":
                lehine, aleyhine = (h[m] - e) / e * 100, (e - l[m]) / e * 100
            else:
                lehine, aleyhine = (e - l[m]) / e * 100, (h[m] - e) / e * 100
            maxl, maxa = max(maxl, lehine), max(maxa, aleyhine)
            if sonuc == "BELIRSIZ":
                if aleyhine >= SL:            # aynı mumda ikisi de olursa zarar sayılır
                    sonuc, dk = "KAYBETTI", (m - j) * 15
                elif lehine >= TP:
                    sonuc, dk = "KAZANDI", (m - j) * 15

        sonuclar.append({
            "sembol": s,
            "tarih": datetime.fromtimestamp(k15[j][0] / 1000, timezone.utc).strftime("%Y-%m-%d %H:%M"),
            "yon": yon, "fiyat": e, "sma": round(s_, 6),
            "mesafe_yuzde": round((e - s_) / s_ * 100, 2),
            "sma_egim": round((s_ - sma[i - 6]) / sma[i - 6] * 100, 2),
            "mum_boy": round(rng / o[j] * 100, 2),
            "alt_fitil": round((min(o[j], c[j]) - l[j]) / rng * 100, 0) if rng else 0,
            "ust_fitil": round((h[j] - max(o[j], c[j])) / rng * 100, 0) if rng else 0,
            "mum_renk": "YESIL" if c[j] >= o[j] else "KIRMIZI",
            "hacim_x": round(v[j] / vol_ort, 1) if vol_ort else 0,
            "gelis_hareket": round(abs(c[j - 1] - c[j - 17]) / c[j - 17] * 100, 2),
            "sonuc": sonuc, "dk": dk,
            "max_lehine": round(maxl, 2), "max_aleyhine": round(maxa, 2)})
    return sonuclar


# ---------- ÖZET ----------
def oran(rows):
    bit = [r for r in rows if r["sonuc"] != "BELIRSIZ"]
    if not bit:
        return f"{len(rows)} sinyal | sonuç yok"
    k = sum(1 for r in bit if r["sonuc"] == "KAZANDI")
    return f"{len(rows)} sinyal | %{k / len(bit) * 100:.0f} kazandı"


def kova(x, sinirlar, adlar):
    for s, a in zip(sinirlar, adlar):
        if x < s:
            return a
    return adlar[-1]


def grup(baslik, rows, fn):
    d = {}
    for r in rows:
        d.setdefault(fn(r), []).append(r)
    metin = f"\n{baslik}\n"
    for ad in sorted(d):
        metin += f"  {ad}: {oran(d[ad])}\n"
    return metin


def ozet(rows, coin_n):
    bit = [r for r in rows if r["sonuc"] != "BELIRSIZ"]
    kaz = sum(1 for r in bit if r["sonuc"] == "KAZANDI")
    basabas = SL / (TP + SL) * 100
    m = (f"📊 4H SMA{SMA_LEN} dokunuş backtest\n"
         f"{GUN} gün | {coin_n} coin | dokunma ±%{TOLERANS} | hedef +%{TP} / stop -%{SL} (24s)\n\n"
         f"Toplam sinyal: {len(rows)} (günde ~{len(rows) / GUN:.0f})\n"
         f"Kazanan: {kaz} | Kaybeden: {len(bit) - kaz} | Belirsiz: {len(rows) - len(bit)}\n"
         f"Kazanma oranı: %{kaz / len(bit) * 100 if bit else 0:.0f} (başabaş: %{basabas:.0f})\n")
    m += grup("Yön", rows, lambda r: r["yon"])
    m += grup("Yön + SMA eğimi (24s)", rows,
              lambda r: r["yon"] + " | SMA " + kova(r["sma_egim"], [-0.5, 0.5], ["aşağı", "yatay", "yukarı"]))
    m += grup("Mum boyu (high-low %)", rows,
              lambda r: kova(r["mum_boy"], [1, 3, 7], ["1) <%1", "2) %1-3", "3) %3-7", "4) %7+"]))
    m += grup("Hacim (20 mum ort. katı)", rows,
              lambda r: kova(r["hacim_x"], [1, 2, 4], ["1) <1x", "2) 1-2x", "3) 2-4x", "4) 4x+"]))
    m += grup("Yöndeki fitil oranı (AL: alt, SAT: üst)", rows,
              lambda r: kova(r["alt_fitil"] if r["yon"] == "AL" else r["ust_fitil"],
                             [20, 40, 60], ["1) <%20", "2) %20-40", "3) %40-60", "4) %60+"]))
    m += grup("SMA'ya geliş hareketi (son 4 saat)", rows,
              lambda r: kova(r["gelis_hareket"], [2, 5, 10], ["1) <%2", "2) %2-5", "3) %5-10", "4) %10+"]))
    m += grup("Mum rengi", rows, lambda r: r["mum_renk"])
    return m


# ---------- ANA ----------
tickerlar = sorted(
    [t for t in get("/fapi/v1/ticker/24hr")
     if t["symbol"].endswith("USDT") and "_" not in t["symbol"]
     and float(t["quoteVolume"]) >= MIN_HACIM],
    key=lambda t: -float(t["quoteVolume"]))[:COIN_SAYISI]

telegram(f"▶️ Backtest başladı: {len(tickerlar)} coin, {GUN} gün (yaklaşık 3-5 dk)")
tum = []
for n, t in enumerate(tickerlar, 1):
    try:
        tum += coin_test(t["symbol"])
    except Exception as e:
        print(t["symbol"], e)
    print(f"{n}/{len(tickerlar)} {t['symbol']} | toplam sinyal: {len(tum)}")
    time.sleep(1.0)

with open(DOSYA, "w", newline="", encoding="utf-8") as f:
    w = csv.DictWriter(f, fieldnames=ALANLAR)
    w.writeheader()
    w.writerows(tum)

if tum:
    telegram(ozet(tum, len(tickerlar)))
    telegram_dosya()
else:
    telegram("Hiç sinyal çıkmadı.")
print("Bitti")
