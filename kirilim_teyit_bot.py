import os, time, requests
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
import numpy as np
import pandas as pd

# ───────── AYARLAR ─────────
TIMEFRAME   = os.environ.get("TIMEFRAME", "15m")
DAYS        = int(os.environ.get("DAYS", 60))              # kaç gün geriye
TOP_N       = int(os.environ.get("TOP_N", 80))             # hacme göre ilk kaç coin
MIN_VOLUME  = float(os.environ.get("MIN_VOLUME_USDT", 3_000_000))
SMA_LEN     = int(os.environ.get("SMA_LEN", 100))
BODY_PCT    = float(os.environ.get("BODY_PCT", 5.0))
HORIZON     = int(os.environ.get("HORIZON", 16))           # 16 mum = 4 saat (15m'de)
TP_PCT      = float(os.environ.get("TP_PCT", 3.0))         # hedef %
SL_PCT      = float(os.environ.get("SL_PCT", 2.0))         # stop %
COOLDOWN    = int(os.environ.get("COOLDOWN_BAR", 4))       # aynı coinde iki sinyal arası min mum
PRE         = 20                                           # "önceki mumlar" penceresi
WORKERS     = int(os.environ.get("MAX_WORKERS", 4))

# 4 özelliğin "kural testi" eşikleri (benim varsayımım, değiştirebilirsiniz)
HACIM_MIN   = float(os.environ.get("HACIM_MIN", 3.0))      # hacim normalin en az kaç katı
GOVDE_MIN   = float(os.environ.get("GOVDE_KAT_MIN", 4.0))  # gövde önceki ort. gövdenin en az kaç katı
EGIM_MIN    = float(os.environ.get("EGIM_MIN", -0.3))      # yöne göre SMA eğimi en az (%)
ALT_MIN     = int(os.environ.get("ALT_MIN", 10))           # kırılım öncesi min mum sayısı

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
            time.sleep(w)
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
            continue
        if r.status_code != 200:
            return None
        try:
            return r.json()
        except Exception:
            return None
    return None


def fetch(symbol):
    start = int((time.time() - DAYS * 86400) * 1000)
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


def analiz_sembol(symbol):
    rows = fetch(symbol)
    if len(rows) < SMA_LEN + PRE + HORIZON + 20:
        return []
    df = pd.DataFrame(rows).iloc[:-1]                      # canlı mumu at
    o, h, l, c, qv = [df[i].astype(float).reset_index(drop=True) for i in (1, 2, 3, 4, 7)]
    t = df[0].astype("int64").reset_index(drop=True)

    sma = c.rolling(SMA_LEN).mean()
    body = (c - o).abs() / o * 100
    prev_c, prev_sma = c.shift(1), sma.shift(1)

    direct = (l <= sma) & (sma <= h)
    near = ((l - sma).abs() / sma <= 0.003) | ((h - sma).abs() / sma <= 0.003)
    bull = (prev_c < prev_sma) & (c > sma)
    bear = (prev_c > prev_sma) & (c < sma)
    sig = (body >= BODY_PCT) & (direct | near | bull | bear) & sma.notna() & prev_sma.notna()

    govde_ort = body.shift(1).rolling(PRE).mean()          # önceki 20 mumun ort. gövdesi
    hacim_ort = qv.shift(1).rolling(PRE).mean()

    O, H, L, C, S = o.values, h.values, l.values, c.values, sma.values
    out, last = [], -10**9
    for i in np.where(sig.values)[0]:
        if i < SMA_LEN + PRE + 11 or i + HORIZON >= len(C) or i - last < COOLDOWN:
            continue
        if not (govde_ort.iloc[i] > 0 and hacim_ort.iloc[i] > 0):
            continue
        last = i
        d = 1 if C[i] > O[i] else -1                       # 1=LONG, -1=SHORT
        entry = C[i]

        # 4 özellik
        govde_kat = body.iloc[i] / govde_ort.iloc[i]                       # 1) sıkışma → patlama
        hacim_kat = qv.iloc[i] / hacim_ort.iloc[i]                         # 2) hacim
        egim = (S[i - 1] / S[i - 11] - 1) * 100 * d                        # 3) SMA eğimi (yöne göre +)
        alt, j = 0, i - 1                                                  # 4) kırılım öncesi karşı tarafta kalış
        while j >= 0 and alt < 200 and not np.isnan(S[j]) and ((C[j] < S[j]) if d == 1 else (C[j] > S[j])):
            alt += 1
            j -= 1

        # sonuç
        fwd = (C[i + HORIZON] / entry - 1) * d * 100
        hi, lo = H[i + 1:i + HORIZON + 1].max(), L[i + 1:i + HORIZON + 1].min()
        if d == 1:
            maxfav, maxadv = (hi / entry - 1) * 100, (lo / entry - 1) * 100
        else:
            maxfav, maxadv = (1 - lo / entry) * 100, (1 - hi / entry) * 100
        tpsl = "YOK"
        for k in range(1, HORIZON + 1):
            if d == 1:
                tp, sl = H[i + k] >= entry * (1 + TP_PCT / 100), L[i + k] <= entry * (1 - SL_PCT / 100)
            else:
                tp, sl = L[i + k] <= entry * (1 - TP_PCT / 100), H[i + k] >= entry * (1 + SL_PCT / 100)
            if sl:                                         # aynı mumda ikisi de olursa SL say (temkinli)
                tpsl = "SL"
                break
            if tp:
                tpsl = "TP"
                break

        out.append(dict(
            tarih=datetime.fromtimestamp(t[i] / 1000, timezone.utc).strftime("%Y-%m-%d %H:%M"),
            sembol=symbol, yon="LONG" if d == 1 else "SHORT", giris=entry,
            govde_pct=round(body.iloc[i], 2), govde_kat=round(govde_kat, 2),
            hacim_kat=round(hacim_kat, 2), sma_egim=round(egim, 3), alt_mum=alt,
            fwd=round(fwd, 2), maxfav=round(maxfav, 2), maxadv=round(maxadv, 2), tpsl=tpsl))
    return out


def ozet(g):
    return {"adet": len(g),
            "kazanma_%": round((g.fwd > 0).mean() * 100, 1),
            "ort_getiri_%": round(g.fwd.mean(), 2),
            "medyan_%": round(g.fwd.median(), 2),
            "ort_max_lehine_%": round(g.maxfav.mean(), 2),
            "ort_max_aleyhine_%": round(g.maxadv.mean(), 2),
            f"TP{TP_PCT:g}_%": round((g.tpsl == "TP").mean() * 100, 1),
            f"SL{SL_PCT:g}_%": round((g.tpsl == "SL").mean() * 100, 1)}


def tablo(df, kolon):
    try:
        gruplar = pd.qcut(df[kolon], 4, duplicates="drop")
    except Exception:
        gruplar = pd.cut(df[kolon], 4)
    satirlar = {str(k): ozet(g) for k, g in df.groupby(gruplar, observed=True)}
    return pd.DataFrame(satirlar).T.to_string()


def rapor(df):
    L = [f"BACKTEST | {TIMEFRAME} | son {DAYS} gün | {df.sembol.nunique()} coin | {len(df)} sinyal",
         f"Sonuç ufku: {HORIZON} mum | TP %{TP_PCT:g} / SL %{SL_PCT:g}", "",
         "══ GENEL ══", pd.DataFrame({"hepsi": ozet(df)}).T.to_string(), ""]
    L += ["══ YÖNE GÖRE ══",
          pd.DataFrame({y: ozet(g) for y, g in df.groupby("yon")}).T.to_string(), ""]
    adlar = [("govde_kat", "1) SIKIŞMA→PATLAMA: sinyal gövdesi / önceki 20 mum ort. gövdesi"),
             ("hacim_kat", "2) HACİM: sinyal hacmi / önceki 20 mum ort. hacmi"),
             ("sma_egim", "3) SMA EĞİMİ: son 10 mumda %, yöne göre (+ = yön lehine)"),
             ("alt_mum", "4) KIRILIM ÖNCESİ: karşı tarafta kaç mum kalmış")]
    for kolon, ad in adlar:
        L += [f"══ {ad} ══", tablo(df, kolon), ""]

    skor = ((df.hacim_kat >= HACIM_MIN).astype(int) + (df.govde_kat >= GOVDE_MIN).astype(int) +
            (df.sma_egim >= EGIM_MIN).astype(int) + (df.alt_mum >= ALT_MIN).astype(int))
    L += [f"══ KURAL TESTİ: kaç şart sağlanıyor? (hacim≥{HACIM_MIN:g}x, gövde≥{GOVDE_MIN:g}x, "
          f"eğim≥{EGIM_MIN:g}%, alt mum≥{ALT_MIN}) ══",
          pd.DataFrame({f"{k} şart": ozet(g) for k, g in df.groupby(skor)}).T.to_string(), "",
          "══ İLK / SON YARI (tutarlılık kontrolü) ══"]
    yari = len(df) // 2
    ds = df.sort_values("tarih")
    L += [pd.DataFrame({"ilk yarı": ozet(ds.iloc[:yari]), "son yarı": ozet(ds.iloc[yari:])}).T.to_string()]
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
    print(f"{len(semboller)} coin taranıyor ({DAYS} gün, {TIMEFRAME})... birkaç dakika sürebilir.", flush=True)

    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        sonuclar = list(ex.map(analiz_sembol, semboller))
    kayitlar = [x for s in sonuclar for x in s]
    if not kayitlar:
        print("Hiç sinyal bulunamadı.")
        return

    df = pd.DataFrame(kayitlar).sort_values("tarih")
    df.to_csv("backtest_sinyaller.csv", index=False, encoding="utf-8-sig")
    metin = rapor(df)
    with open("backtest_analiz.txt", "w", encoding="utf-8") as f:
        f.write(metin)
    print("\n" + metin)
    print("\nDosyalar: backtest_sinyaller.csv, backtest_analiz.txt")
    tg_file("backtest_analiz.txt")
    tg_file("backtest_sinyaller.csv")


if __name__ == "__main__":
    main()
