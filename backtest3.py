import os, time, requests
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
import numpy as np
import pandas as pd

# ───────── AYARLAR ─────────
TIMEFRAME  = os.environ.get("TIMEFRAME", "15m")
DAYS       = int(os.environ.get("DAYS", 20))
TOP_N      = int(os.environ.get("TOP_N", 400))
MIN_VOLUME = float(os.environ.get("MIN_VOLUME_USDT", 1_000_000))
MIN_BODY   = float(os.environ.get("MIN_BODY", 3.0))      # gövde en az %3
BB_LEN     = int(os.environ.get("BB_LEN", 20))
BB_STD     = float(os.environ.get("BB_STD", 2.0))
SMA_LEN    = int(os.environ.get("SMA_LEN", 100))
HORIZON    = int(os.environ.get("HORIZON", 16))          # 16 mum = 4 saat
TP1, SL1   = float(os.environ.get("TP1", 3.0)), float(os.environ.get("SL1", 2.0))
TP2, SL2   = float(os.environ.get("TP2", 5.0)), float(os.environ.get("SL2", 5.0))
COOLDOWN   = int(os.environ.get("COOLDOWN_BAR", 4))
WORKERS    = int(os.environ.get("MAX_WORKERS", 3))

API   = "https://fapi.binance.com"
TOKEN = os.environ.get("TELEGRAM_TOKEN", "BURAYA_TOKEN")
CHAT  = os.environ.get("TELEGRAM_CHAT_ID", "BURAYA_CHAT_ID")

http = requests.Session()
blocked_until = 0.0


def get(path, params=None):
    global blocked_until
    for _ in range(5):
        w = blocked_until - time.time()
        if w > 0:
            print(f"Binance limiti, {int(w)} sn bekleniyor...", flush=True)
            time.sleep(min(w, 30))
            continue
        try:
            r = http.get(API + path, params=params, timeout=15)
        except Exception:
            time.sleep(2)
            continue
        if r.status_code in (418, 429):
            try:
                w = float(r.headers.get("Retry-After"))
            except (TypeError, ValueError):
                w = 120 if r.status_code == 418 else 30
            blocked_until = max(blocked_until, time.time() + w + 2)
            print(f"HTTP {r.status_code}, {int(w)} sn engel", flush=True)
            continue
        if r.status_code != 200:
            return None
        try:
            return r.json()
        except Exception:
            return None
    return None


def fetch(symbol):
    start = int((time.time() - (DAYS + 2) * 86400) * 1000)   # +2 gün: SMA ısınması için
    rows = []
    while True:
        data = get("/fapi/v1/klines", {"symbol": symbol, "interval": TIMEFRAME,
                                       "startTime": start, "limit": 1500})
        if not isinstance(data, list) or not data:
            break
        rows += data
        if len(data) < 1500:
            break
        start = data[-1][0] + 1
    return rows


def tpsl(H, L, i, d, entry, tp, sl):
    for k in range(1, HORIZON + 1):
        if d == 1:
            hit_tp, hit_sl = H[i + k] >= entry * (1 + tp / 100), L[i + k] <= entry * (1 - sl / 100)
        else:
            hit_tp, hit_sl = L[i + k] <= entry * (1 - tp / 100), H[i + k] >= entry * (1 + sl / 100)
        if hit_sl:                       # aynı mumda ikisi de olursa SL say (temkinli)
            return "SL"
        if hit_tp:
            return "TP"
    return "YOK"


def analiz_sembol(symbol):
    rows = fetch(symbol)
    if len(rows) < SMA_LEN + HORIZON + 30:
        return []
    df = pd.DataFrame(rows).iloc[:-1]                         # canlı mumu at
    o, h, l, c = [df[i].astype(float).reset_index(drop=True) for i in (1, 2, 3, 4)]
    t = df[0].astype("int64").reset_index(drop=True)

    sma = c.rolling(SMA_LEN).mean()
    mid = c.rolling(BB_LEN).mean()
    sd = c.rolling(BB_LEN).std(ddof=0)
    up, lo = mid + BB_STD * sd, mid - BB_STD * sd
    body = (c - o).abs() / o * 100
    green = c > o

    bb = (body >= MIN_BODY) & ((green & (c > up)) | (~green & (c < lo)))
    sm = (body >= MIN_BODY) & (l <= sma) & (sma <= h) & ((green & (c > sma)) | (~green & (c < sma)))

    cut = (time.time() - DAYS * 86400) * 1000
    O, H, L, C = o.values, h.values, l.values, c.values
    out, last = [], -10**9
    for i in np.where((bb | sm).values)[0]:
        if i < SMA_LEN or i + HORIZON >= len(C) or t[i] < cut or i - last < COOLDOWN:
            continue
        last = i
        d = 1 if C[i] > O[i] else -1
        entry = C[i]
        fwd = (C[i + HORIZON] / entry - 1) * d * 100
        hi, low = H[i + 1:i + HORIZON + 1].max(), L[i + 1:i + HORIZON + 1].min()
        if d == 1:
            maxfav, maxadv = (hi / entry - 1) * 100, (low / entry - 1) * 100
        else:
            maxfav, maxadv = (1 - low / entry) * 100, (1 - hi / entry) * 100
        out.append(dict(
            tarih=datetime.fromtimestamp(t[i] / 1000, timezone.utc).strftime("%Y-%m-%d %H:%M"),
            sembol=symbol, yon="LONG" if d == 1 else "SHORT", giris=entry,
            govde_pct=round(body.iloc[i], 2),
            bb=int(bb.iloc[i]), sma=int(sm.iloc[i]),
            sma_uzak=round(abs(C[i] / sma.iloc[i] - 1) * 100, 2),
            bb_disi=round(((C[i] - up.iloc[i]) / up.iloc[i] * 100) if d == 1
                          else ((lo.iloc[i] - C[i]) / lo.iloc[i] * 100), 2),
            fwd=round(fwd, 2), maxfav=round(maxfav, 2), maxadv=round(maxadv, 2),
            r1=tpsl(H, L, i, d, entry, TP1, SL1), r2=tpsl(H, L, i, d, entry, TP2, SL2)))
    return out


def ozet(g):
    if len(g) == 0:
        return {"adet": 0}
    return {"adet": len(g),
            "kazanma_%": round((g.fwd > 0).mean() * 100, 1),
            "ort_getiri_%": round(g.fwd.mean(), 2),
            "medyan_%": round(g.fwd.median(), 2),
            "ort_max_lehine_%": round(g.maxfav.mean(), 2),
            "ort_max_aleyhine_%": round(g.maxadv.mean(), 2),
            f"TP{TP1:g}/SL{SL1:g}_TP%": round((g.r1 == "TP").mean() * 100, 1),
            f"TP{TP1:g}/SL{SL1:g}_SL%": round((g.r1 == "SL").mean() * 100, 1),
            f"TP{TP2:g}/SL{SL2:g}_TP%": round((g.r2 == "TP").mean() * 100, 1),
            f"TP{TP2:g}/SL{SL2:g}_SL%": round((g.r2 == "SL").mean() * 100, 1)}


def tablo(d):
    return pd.DataFrame({k: ozet(g) for k, g in d.items()}).T.to_string()


def rapor(df):
    gruplar = {
        "BB hepsi": df[df.bb == 1],
        "SMA100 hepsi": df[df.sma == 1],
        "İKİSİ BİRDEN": df[(df.bb == 1) & (df.sma == 1)],
        "sadece BB": df[(df.bb == 1) & (df.sma == 0)],
        "sadece SMA100": df[(df.bb == 0) & (df.sma == 1)],
    }
    L = [f"BACKTEST 3 | {TIMEFRAME} | son {DAYS} gün | {df.sembol.nunique()} coin | {len(df)} sinyal",
         f"Gövde ≥ %{MIN_BODY:g} | BB({BB_LEN},{BB_STD:g}) | SMA{SMA_LEN} | Ufuk: {HORIZON} mum",
         f"Sonuç 1: TP %{TP1:g}/SL %{SL1:g} | Sonuç 2: TP %{TP2:g}/SL %{SL2:g}", "",
         "══ 1) GRUPLAR ══", tablo(gruplar), ""]

    yon = {f"{ad} | {y}": g[g.yon == y] for ad, g in gruplar.items() for y in ("LONG", "SHORT")}
    L += ["══ 2) GRUP × YÖN ══", tablo(yon), ""]

    dilim = pd.cut(df.govde_pct, [0, 4, 6, 10, 1000], labels=["3-4%", "4-6%", "6-10%", "10%+"])
    L += ["══ 3) GÖVDE BÜYÜKLÜĞÜ (tüm sinyaller) ══",
          tablo({str(k): g for k, g in df.groupby(dilim, observed=True)}), ""]

    bbg = gruplar["BB hepsi"]
    if len(bbg) >= 8:
        try:
            q = pd.qcut(bbg.bb_disi, 4, duplicates="drop")
            L += ["══ 4) BB'NİN NE KADAR DIŞINDA KAPANDI (% , sadece BB grubu) ══",
                  tablo({str(k): g for k, g in bbg.groupby(q, observed=True)}), ""]
        except Exception:
            pass

    L += ["══ 5) TUTARLILIK: ilk yarı / son yarı ══"]
    ds = df.sort_values("tarih")
    yari = len(ds) // 2
    for ad, mask in [("BB hepsi", ds.bb == 1), ("SMA100 hepsi", ds.sma == 1),
                     ("İKİSİ BİRDEN", (ds.bb == 1) & (ds.sma == 1))]:
        a, b = ds.iloc[:yari][mask.iloc[:yari]], ds.iloc[yari:][mask.iloc[yari:]]
        L += [f"-- {ad}", tablo({"ilk yarı": a, "son yarı": b})]
    return "\n".join(L)


def tg_file(path):
    if TOKEN == "BURAYA_TOKEN":
        return
    try:
        with open(path, "rb") as f:
            http.post(f"https://api.telegram.org/bot{TOKEN}/sendDocument",
                      data={"chat_id": CHAT}, files={"document": f}, timeout=30)
    except Exception as e:
        print(f"Telegram hatası: {e}")


def main():
    t = get("/fapi/v1/ticker/24hr")
    if not isinstance(t, list):
        print("Ticker alınamadı.")
        return
    semboller = [d["symbol"] for d in sorted(t, key=lambda x: -float(x.get("quoteVolume", 0)))
                 if d["symbol"].endswith("USDT") and float(d.get("quoteVolume", 0)) >= MIN_VOLUME][:TOP_N]
    print(f"{len(semboller)} coin taranıyor ({DAYS} gün, {TIMEFRAME})...", flush=True)

    sonuclar = []
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = [ex.submit(analiz_sembol, s) for s in semboller]
        for n, f in enumerate(as_completed(futs), 1):
            sonuclar.append(f.result())
            if n % 10 == 0 or n == len(semboller):
                print(f"{n}/{len(semboller)} coin bitti", flush=True)

    kayitlar = [x for s in sonuclar for x in s]
    if not kayitlar:
        print("Hiç sinyal bulunamadı.")
        return

    df = pd.DataFrame(kayitlar).sort_values("tarih")
    df.to_csv("backtest3_sinyaller.csv", index=False, encoding="utf-8-sig")
    metin = rapor(df)
    with open("backtest3_analiz.txt", "w", encoding="utf-8") as f:
        f.write(metin)
    print("\n" + metin)
    print("\nDosyalar: backtest3_sinyaller.csv, backtest3_analiz.txt")
    tg_file("backtest3_analiz.txt")
    tg_file("backtest3_sinyaller.csv")


if __name__ == "__main__":
    main()
