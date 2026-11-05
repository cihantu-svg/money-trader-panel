import os, sys, time, csv, math, requests
from bisect import bisect_left
from datetime import datetime, timezone

BASE = "https://fapi.binance.com"

# ---------- genel ----------
GUN = 90                  # kaç günlük geçmiş test edilsin
ISINMA_GUN = 2            # gösterge ısınması için ekstra gün (bu günlerde olay sayılmaz)
MIN_HACIM = 1_000_000     # 24s USDT hacim alt sınırı
TOP_N = 400               # hacme göre en fazla kaç coin
MUM_YUZDE = 7             # 15dk mum boyu (yüksek-düşük) en az %
ATLA_MUM = 16             # aynı coinde iki işlem arası min 15dk mum (16 = 4 saat)
# ---------- çıkış (tersine işlem) ----------
SL = 5.0                  # stop %
TP1 = 10.0                # 1. hedef % (pozisyonun yarısı)
TP2 = 20.0                # 2. hedef % (kalan yarı)
TUT_MUM = 192             # işlemde max kalma (192 x 15dk = 48 saat)
UCRET = 0.1               # gidiş-dönüş komisyon %
# ---------- kırılım mumu (Pine kodundaki ayarlar) ----------
BO_LEN = 20
BO_TAMPON = 0.5
HACIM_CARPAN = 2.0
RSI_LEN, RSI_AL, RSI_SAT = 14, 60, 40
MIN_ARA = 5               # kırılım sinyalleri arası min mum
# ---------- 4H OB ----------
OB_YAKIN = 5.0            # OB bölgesine max yakınlık %
SON_OB = 5                # indikatör gibi: en yeni 5 iç OB
# ---------- delta tetikleyici ----------
DELTA_BASLA = 2           # olay mumundan kaç mum sonra başlasın (2 = olay mumu ve sonraki mum atlanır)
DELTA_ADET = 3            # kaç mum boyunca aransın
H4 = 4 * 3600 * 1000

oturum = requests.Session()
TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT = os.getenv("TELEGRAM_CHAT_ID")
satirlar = []

def yaz(m):
    print(m)
    satirlar.append(m)

def telegram(msg):
    try:
        r = oturum.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                        data={"chat_id": CHAT, "text": msg, "parse_mode": "HTML"}, timeout=15)
        if r.status_code != 200:
            print("TELEGRAM HATA:", r.status_code, r.text)
    except Exception as e:
        print("telegram hata:", e)

def telegram_dosya(yol):
    try:
        with open(yol, "rb") as f:
            oturum.post(f"https://api.telegram.org/bot{TOKEN}/sendDocument",
                        data={"chat_id": CHAT}, files={"document": f}, timeout=60)
    except Exception as e:
        print("dosya hata:", e)

def telegrama_gonder(baslik, csv_yolu):
    if not (TOKEN and CHAT):
        print("TELEGRAM_TOKEN / TELEGRAM_CHAT_ID tanımlı değil, Telegram'a gönderilmedi")
        return
    parca = ""
    for satir in satirlar:
        if len(parca) + len(satir) > 3500:
            telegram("<pre>" + parca + "</pre>")
            parca = ""
        parca += satir + "\n"
    telegram(baslik + "\n<pre>" + parca + "</pre>")
    if csv_yolu and os.path.exists(csv_yolu):
        telegram_dosya(csv_yolu)

def get(path, **p):
    for _ in range(2):
        r = oturum.get(BASE + path, params=p, timeout=15)
        if r.status_code in (418, 429):
            time.sleep(int(r.headers.get("Retry-After", 60)))
            continue
        r.raise_for_status()
        return r.json()
    raise RuntimeError("Rate limit devam ediyor")

def mumlar_15dk(s):
    bitis = int(time.time() * 1000)
    hedef = bitis - (GUN + ISINMA_GUN) * 86400 * 1000
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
    return out[:-1]   # kapanmamış son mumu at

def mumlar_4h(s):
    k = get("/fapi/v1/klines", symbol=s, interval="4h", limit=1500)
    time.sleep(0.5)
    return k[:-1]     # kapanmamış son 4H mumu at

# ---------- Pine'daki 4H iç OB mantığı ----------
class Pv:
    def __init__(self):
        self.cur, self.last, self.crossed, self.idx = None, None, False, 0

def esit_degil(a, b):
    return True if (a is None or b is None) else a != b

def ob_gecmisi(k4):
    """Her 4H mum kapanışından sonra aktif en yeni SON_OB iç OB: (ust, alt, yon) yon +1 yükseliş, -1 düşüş"""
    n = len(k4)
    T = [x[0] for x in k4]
    H = [float(x[2]) for x in k4]
    L = [float(x[3]) for x in k4]
    C = [float(x[4]) for x in k4]
    tr = [H[0] - L[0]] + [max(H[i] - L[i], abs(H[i] - C[i - 1]), abs(L[i] - C[i - 1])) for i in range(1, n)]
    atr = [None] * n
    if n >= 200:
        a = sum(tr[:200]) / 200
        atr[199] = a
        for i in range(200, n):
            a = (a * 199 + tr[i]) / 200
            atr[i] = a
    PH, PL = [], []
    for i in range(n):
        vol = atr[i] is not None and (H[i] - L[i]) >= 2 * atr[i]
        PH.append(L[i] if vol else H[i])
        PL.append(H[i] if vol else L[i])
    ih, il, sh, sl = Pv(), Pv(), Pv(), Pv()
    leg, onceki = {5: 0, 50: 0}, {5: 0, 50: 0}
    prev_ih = prev_il = None
    obs, snaps = [], []
    for i in range(n):
        for size, hp, lp in ((50, sh, sl), (5, ih, il)):
            if i >= size:
                if H[i - size] > max(H[i - size + 1:i + 1]):
                    leg[size] = 0
                elif L[i - size] < min(L[i - size + 1:i + 1]):
                    leg[size] = 1
            degisim = leg[size] - onceki[size]
            onceki[size] = leg[size]
            if degisim == 1:
                lp.last, lp.cur, lp.crossed, lp.idx = lp.cur, L[i - size], False, i - size
            elif degisim == -1:
                hp.last, hp.cur, hp.crossed, hp.idx = hp.cur, H[i - size], False, i - size
        if i > 0:
            yukari = (ih.cur is not None and prev_ih is not None and C[i] > ih.cur and C[i - 1] <= prev_ih)
            if yukari and not ih.crossed and esit_degil(ih.cur, sh.cur):
                ih.crossed = True
                a = PL[ih.idx:i]
                if a:
                    j = ih.idx + a.index(min(a))
                    if len(obs) >= 100:
                        obs.pop()
                    obs.insert(0, (PH[j], PL[j], 1))
            asagi = (il.cur is not None and prev_il is not None and C[i] < il.cur and C[i - 1] >= prev_il)
            if asagi and not il.crossed and esit_degil(il.cur, sl.cur):
                il.crossed = True
                a = PH[il.idx:i]
                if a:
                    j = il.idx + a.index(max(a))
                    if len(obs) >= 100:
                        obs.pop()
                    obs.insert(0, (PH[j], PL[j], -1))
        prev_ih, prev_il = ih.cur, il.cur
        obs = [o for o in obs if not ((o[2] == -1 and H[i] > o[0]) or (o[2] == 1 and L[i] < o[1]))]
        snaps.append(tuple(obs[:SON_OB]))
    return T, snaps

def ob_kontrol(s):
    """Grafikle karşılaştırmak için: son 4H kapanışındaki aktif OB bölgeleri"""
    T, snaps = ob_gecmisi(mumlar_4h(s))
    yaz(f"{s} 4H aktif iç OB'ler (en yeni önce):")
    for ust, alt, yon in snaps[-1]:
        yaz(f"  {'DÜŞÜŞ (kırmızı)' if yon == -1 else 'YÜKSELİŞ (mavi)'}: {alt:.6g} - {ust:.6g}")
    if not snaps[-1]:
        yaz("  (aktif OB yok)")

def ob_yakin(yon, h, l, snap):
    """Olay mumunun OB bölgesine mesafesi % (0 = temas). Yoksa None.
    SHORT (yeşil mum): üstteki düşüş OB. LONG (kırmızı mum): alttaki yükseliş OB. OB delinmemiş olmalı."""
    en_iyi = None
    for ust, alt, by in snap:
        if yon == "SHORT":
            if by != -1 or h > ust:
                continue
            d = 0.0 if h >= alt else (alt - h) / h * 100
        else:
            if by != 1 or l < alt:
                continue
            d = 0.0 if l <= ust else (l - ust) / l * 100
        if d <= OB_YAKIN and (en_iyi is None or d < en_iyi):
            en_iyi = d
    return en_iyi

# ---------- Pine'daki kırılım mumu ----------
def ema_serisi(x, n):
    k = 2 / (n + 1)
    out = [x[0]]
    for v in x[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out

def rsi_serisi(c, n):
    r = [None] * len(c)
    if len(c) <= n:
        return r
    kaz = [0.0] + [max(c[i] - c[i - 1], 0) for i in range(1, len(c))]
    kay = [0.0] + [max(c[i - 1] - c[i], 0) for i in range(1, len(c))]
    ak, ay = sum(kaz[1:n + 1]) / n, sum(kay[1:n + 1]) / n
    r[n] = 100.0 if ay == 0 else 100 - 100 / (1 + ak / ay)
    for i in range(n + 1, len(c)):
        ak, ay = (ak * (n - 1) + kaz[i]) / n, (ay * (n - 1) + kay[i]) / n
        r[i] = 100.0 if ay == 0 else 100 - 100 / (1 + ak / ay)
    return r

def kirilim_sinyalleri(h, l, c, v):
    """Pine: breakout + hacim 2x + RSI 60/40 + MACD, sinyaller arası min 5 mum. (al[], sat[])"""
    n = len(c)
    rsi = rsi_serisi(c, RSI_LEN)
    macd = [a - b for a, b in zip(ema_serisi(c, 12), ema_serisi(c, 26))]
    sig = ema_serisi(macd, 9)
    al, sat, son = [False] * n, [False] * n, None
    for i in range(max(BO_LEN + 1, 20), n):
        if rsi[i] is None:
            continue
        direnc, destek = max(h[i - BO_LEN:i]), min(l[i - BO_LEN:i])
        hacim_ok = v[i] > (sum(v[i - 19:i + 1]) / 20) * HACIM_CARPAN
        hist = macd[i] - sig[i]
        yukari = c[i] > direnc * (1 + BO_TAMPON / 100) and c[i - 1] <= direnc
        asagi = c[i] < destek * (1 - BO_TAMPON / 100) and c[i - 1] >= destek
        ham_al = yukari and hacim_ok and rsi[i] > RSI_AL and hist > 0 and macd[i] > sig[i]
        ham_sat = asagi and hacim_ok and rsi[i] < RSI_SAT and hist < 0 and macd[i] < sig[i]
        can = son is None or i - son >= MIN_ARA
        if ham_al and can:
            al[i] = True
        if ham_sat and can:
            sat[i] = True
        if (ham_al or ham_sat) and can:
            son = i
    return al, sat

# ---------- işlem simülasyonu (15dk mumlarla) ----------
def cikis_bul(h, l, c, bas, giris, yon):
    sg = 1 if yon == "LONG" else -1
    son = bas + TUT_MUM
    if son > len(c):
        return None                               # 48 saatlik gelecek verisi yok: işlem sayılmaz
    tp1_alindi = False
    for j in range(bas, son):
        fav = h[j] if sg == 1 else l[j]
        adv = l[j] if sg == 1 else h[j]
        fav_p = sg * (fav - giris) / giris * 100
        adv_p = sg * (adv - giris) / giris * 100
        if adv_p <= -SL:                          # önce stop kontrolü (kötümser)
            return (0.5 * TP1 - 0.5 * SL, "TP1_STOP") if tp1_alindi else (-SL, "STOP")
        if fav_p >= TP2:
            return 0.5 * TP1 + 0.5 * TP2, "TP2"
        if not tp1_alindi and fav_p >= TP1:
            tp1_alindi = True
    son_p = sg * (c[son - 1] - giris) / giris * 100
    return (0.5 * TP1 + 0.5 * son_p, "TP1_SURE") if tp1_alindi else (son_p, "SURE")

# ---------- varyantlar: kırılım (K), OB (O), delta (D: None / uyumsuz / ters) ----------
VARYANTLAR = [(k, o, d) for d in (None, "uyumsuz", "ters") for k in (False, True) for o in (False, True)]

def ad(k, o, d):
    p = (["kirilim"] if k else []) + (["OB"] if o else []) + ([f"delta-{d}"] if d else [])
    return "+".join(p) if p else "taban"

def coin_isle(s, k15, k4):
    n = len(k15)
    ISINMA = ISINMA_GUN * 96
    if n < ISINMA + 300:
        return [], {}
    t = [x[0] for x in k15]
    o = [float(x[1]) for x in k15]
    h = [float(x[2]) for x in k15]
    l = [float(x[3]) for x in k15]
    c = [float(x[4]) for x in k15]
    v = [float(x[5]) for x in k15]
    d = [2 * float(x[10]) - float(x[7]) for x in k15]      # delta = 2*taker alış - toplam (USDT)
    al, sat = kirilim_sinyalleri(h, l, c, v)
    h4_t, snaps = ob_gecmisi(k4) if len(k4) >= 60 else ([], [])

    olaylar = []
    for i in range(ISINMA, n - 1):
        if (h[i] - l[i]) / l[i] * 100 < MUM_YUZDE or c[i] == o[i]:
            continue
        yon = "SHORT" if c[i] > o[i] else "LONG"           # tersine işlem: yeşil mum -> SHORT, kırmızı mum -> LONG
        kir = al[i] if yon == "SHORT" else sat[i]
        ob_d = None
        n4 = bisect_left(h4_t, t[i] // H4 * H4) if h4_t else 0
        if n4 >= 1:
            ob_d = ob_yakin(yon, h[i], l[i], snaps[n4 - 1])
        dt = {"ters": None, "uyumsuz": None}
        for j in range(i + DELTA_BASLA, min(i + DELTA_BASLA + DELTA_ADET, n)):
            ters = d[j] < 0 if yon == "SHORT" else d[j] > 0            # delta işareti tersine yönde
            uyumsuz = ters and (c[j] > o[j] if yon == "SHORT" else c[j] < o[j])   # mum rengi ile delta zıt
            if ters and dt["ters"] is None:
                dt["ters"] = j
            if uyumsuz and dt["uyumsuz"] is None:
                dt["uyumsuz"] = j
        olaylar.append({"i": i, "yon": yon, "kir": kir, "ob": ob_d, "dt": dt})

    sayac = {"olay": len(olaylar), "kirilim": sum(1 for e in olaylar if e["kir"]),
             "ob": sum(1 for e in olaylar if e["ob"] is not None),
             "kirilim_ob": sum(1 for e in olaylar if e["kir"] and e["ob"] is not None)}
    satir = []
    for (K, O, D) in VARYANTLAR:
        son_i = -999
        for e in olaylar:
            if (K and not e["kir"]) or (O and e["ob"] is None):
                continue
            giris_i = e["i"] if D is None else e["dt"][D]
            if giris_i is None or e["i"] - son_i < ATLA_MUM:
                continue
            r = cikis_bul(h, l, c, giris_i + 1, c[giris_i], e["yon"])
            if r is None:
                continue
            getiri, tip = r
            son_i = e["i"]
            satir.append({"sembol": s, "varyant": ad(K, O, D), "yon": e["yon"],
                          "tarih": datetime.fromtimestamp(t[e["i"]] / 1000, timezone.utc).strftime("%Y-%m-%d %H:%M"),
                          "mum_yuzde": round((h[e["i"]] - l[e["i"]]) / l[e["i"]] * 100, 1),
                          "kirilim": int(e["kir"]), "ob_mesafe": "" if e["ob"] is None else round(e["ob"], 2),
                          "giris_gecikme": giris_i - e["i"], "giris": c[giris_i],
                          "sonuc": tip, "getiri": round(getiri - UCRET, 2)})
    return satir, sayac

def ozet_satiri(ad_, yf, x):
    m = len(x)
    if m == 0:
        return f"{ad_:<24} {yf:>6} {0:>6}"
    g = [r["getiri"] for r in x]
    ort = sum(g) / m
    sd = math.sqrt(sum((a - ort) ** 2 for a in g) / (m - 1)) if m > 1 else 0
    t = ort / (sd / math.sqrt(m)) if sd > 0 else 0
    tp1 = sum(1 for r in x if r["sonuc"] in ("TP1_STOP", "TP1_SURE", "TP2")) / m * 100
    tp2 = sum(1 for r in x if r["sonuc"] == "TP2") / m * 100
    stp = sum(1 for r in x if r["sonuc"] == "STOP") / m * 100
    return f"{ad_:<24} {yf:>6} {m:>6} {tp1:>6.0f} {tp2:>5.0f} {stp:>6.0f} {ort:>8.2f} {t:>5.1f} {sum(g):>9.1f}"

def main():
    kontrol = os.getenv("OB_KONTROL") or (sys.argv[1] if len(sys.argv) > 1 else None)
    if kontrol:                       # sadece OB bölgelerini yazdır (grafikle karşılaştırmak için)
        ob_kontrol(kontrol)
        telegrama_gonder("🔎 OB kontrol", "")
        return

    ts = [t for t in get("/fapi/v1/ticker/24hr")
          if t["symbol"].endswith("USDT") and "_" not in t["symbol"]
          and float(t["quoteVolume"]) >= MIN_HACIM]
    ts.sort(key=lambda t: float(t["quoteVolume"]), reverse=True)
    semboller = [t["symbol"] for t in ts[:TOP_N]]

    tum, toplam = [], {"olay": 0, "kirilim": 0, "ob": 0, "kirilim_ob": 0}
    for n, s in enumerate(semboller, 1):
        try:
            satir, sayac = coin_isle(s, mumlar_15dk(s), mumlar_4h(s))
            tum += satir
            for k_, v_ in sayac.items():
                toplam[k_] += v_
            print(f"{n}/{len(semboller)} {s} | olay: {toplam['olay']} | işlem: {len(tum)}")
        except Exception as e:
            print(s, e)

    yaz(f"%{MUM_YUZDE}+ 15dk mum: {toplam['olay']} | kırılım mumu: {toplam['kirilim']} | "
        f"4H OB yakın: {toplam['ob']} | ikisi birden: {toplam['kirilim_ob']}")
    yaz(f"Tersine işlem | SL %{SL} | TP1 %{TP1} (yarı) | TP2 %{TP2} (yarı) | {GUN} gün | 15dk mumla simülasyon")
    yaz(f"\n{'varyant':<24} {'yon':>6} {'islem':>6} {'tp1+%':>6} {'tp2%':>5} {'stop%':>6} {'ort.get%':>8} {'t':>5} {'toplam%':>9}")
    for (K, O, D) in VARYANTLAR:
        for yf in ("hepsi", "SHORT", "LONG"):
            x = [r for r in tum if r["varyant"] == ad(K, O, D) and (yf == "hepsi" or r["yon"] == yf)]
            yaz(ozet_satiri(ad(K, O, D), yf, x))
        yaz("")
    yaz("delta-uyumsuz: mum rengi ile delta zıt (SHORT: yeşil mum+negatif delta, LONG: kırmızı mum+pozitif delta)")
    yaz("delta-ters: sadece delta işareti tersine (SHORT: negatif, LONG: pozitif)")
    yaz(f"Delta penceresi: olay mumundan {DELTA_BASLA} mum sonra başlar, {DELTA_ADET} mum bakılır")

    if tum:
        with open("backtest_karsit_islemler.csv", "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(tum[0].keys()))
            w.writeheader()
            w.writerows(tum)
        print("\nTüm işlemler: backtest_karsit_islemler.csv")

    telegrama_gonder("📊 Backtest (tersine işlem, filtreler) bitti", "backtest_karsit_islemler.csv")

if __name__ == "__main__":
    main()
    if os.getenv("RENDER"):   # Render'da program çıkarsa servis yeniden başlar ve backtest'i baştan çalıştırır
        print("Bitti. Servisi durdurabilirsin.")
        while True:
            time.sleep(3600)
