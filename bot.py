import os, time, csv, threading
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
import pandas as pd
import requests

try:
    from zoneinfo import ZoneInfo
except Exception:
    ZoneInfo = None

TIMEFRAME    = os.environ.get("TIMEFRAME", "5m")
LOOK         = int(os.environ.get("LOOK", 6))                 # 6 mum = 30 dk
KAT          = float(os.environ.get("KAT", 5.0))              # net para, normalin kaç katı
MIN_OI_PCT   = float(os.environ.get("MIN_OI_PCT", 1.5))       # OI artışı, %
MIN_MOVE_PCT = float(os.environ.get("MIN_MOVE_PCT", 0.8))     # fiyat hareketi, %
SMA_LEN      = int(os.environ.get("SMA_LEN", 100))
MAX_SMA_PCT  = float(os.environ.get("MAX_SMA_PCT", 3.0))      # SMA'ya max uzaklık, %
KIR_ZORUNLU  = int(os.environ.get("KIR_ZORUNLU", 1))          # 1: SMA kırılımı şart
KIR_BAR      = int(os.environ.get("KIR_BAR", 12))             # kırılım kaç mum içinde olsun
COOLDOWN_MIN = int(os.environ.get("COOLDOWN_MIN", 60))
MIN_VOLUME   = float(os.environ.get("MIN_VOLUME_USDT", 10_000_000))
SCAN_SEC     = int(os.environ.get("SCAN_INTERVAL_SEC", 180))
WORKERS      = int(os.environ.get("MAX_WORKERS", 6))

# Gün sonu raporu ayarları
REPORT_HOUR   = int(os.environ.get("REPORT_HOUR", 21))
REPORT_MINUTE = int(os.environ.get("REPORT_MINUTE", 0))
REPORT_TZ     = os.environ.get("REPORT_TZ", "Europe/London")
SIGNALS_FILE  = os.environ.get("SIGNALS_FILE", "signals_akis.csv")

API   = "https://fapi.binance.com"
TOKEN = os.environ.get("TELEGRAM_TOKEN", "BURAYA_TOKEN")
CHAT  = os.environ.get("TELEGRAM_CHAT_ID", "BURAYA_CHAT_ID")

try:
    TZ = ZoneInfo(REPORT_TZ) if ZoneInfo else timezone.utc
except Exception:
    TZ = timezone.utc

http = requests.Session()
son = {}
blocked_until = 0.0
csv_lock = threading.Lock()
son_rapor_tarihi = None


def tg(text):
    if TOKEN == "BURAYA_TOKEN":
        print(text, flush=True)
        return
    try:
        r = http.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                      data={"chat_id": CHAT, "text": text, "parse_mode": "HTML"}, timeout=10)
        if r.status_code != 200:
            print(f"Telegram HATA {r.status_code}: {r.text}", flush=True)
    except Exception as e:
        print(f"Telegram hatasi: {e}", flush=True)


def tg_file(path, caption=""):
    if TOKEN == "BURAYA_TOKEN":
        return
    try:
        with open(path, "rb") as f:
            http.post(f"https://api.telegram.org/bot{TOKEN}/sendDocument",
                      data={"chat_id": CHAT, "caption": caption},
                      files={"document": f}, timeout=30)
    except Exception as e:
        print(f"Telegram dosya hatasi: {e}", flush=True)


def get(path, params=None):
    global blocked_until
    if time.time() < blocked_until:
        return None
    try:
        r = http.get(API + path, params=params, timeout=10)
    except Exception as e:
        print(f"İstek hatası: {e}", flush=True)
        return None
    if r.status_code in (418, 429):
        try:
            w = float(r.headers.get("Retry-After"))
        except (TypeError, ValueError):
            w = 300 if r.status_code == 418 else 60
        blocked_until = max(blocked_until, time.time() + w + 5)
        tg(f"⛔ Binance {r.status_code}, bot {int(w)} sn duruyor.")
        return None
    if r.status_code != 200:
        print(f"API {r.status_code}: {r.text[:150]}", flush=True)
        return None
    try:
        return r.json()
    except Exception:
        return None


def para(x):
    a = abs(x)
    return ("-" if x < 0 else "+") + (f"{a/1e6:.2f}M" if a >= 1e6 else f"{a/1e3:.0f}K")


# ─────────────── SİNYAL KAYDI ───────────────
SIGNAL_COLS = ["ts_ms", "sembol", "yon", "giris_fiyat", "net_para_usdt",
               "kat", "oi_pct", "sma_fark_pct", "hareket_pct"]


def log_signal(s):
    with csv_lock:
        yeni = not os.path.exists(SIGNALS_FILE)
        with open(SIGNALS_FILE, "a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if yeni:
                w.writerow(SIGNAL_COLS)
            w.writerow([int(time.time() * 1000), s["symbol"], s["yon"], s["fiyat"],
                        round(s["net"]), round(s["kat"], 2), round(s["oi"], 2),
                        round(s["fark"], 2), round(s["hareket"], 2)])


# ─────────────── GÜN SONU RAPORU ───────────────
def rapor_dosya_adi(tarih):
    return f"gun_sonu_akis_{tarih.strftime('%Y-%m-%d')}.csv"


def extremler(symbol, ts_ms):
    """Sinyalden bu yana en yüksek high, en düşük low ve zamanları."""
    data = get("/fapi/v1/klines", {"symbol": symbol, "interval": TIMEFRAME,
                                   "startTime": int(ts_ms), "limit": 1500})
    if not isinstance(data, list) or not data:
        return None
    hi = max(data, key=lambda k: float(k[2]))
    lo = min(data, key=lambda k: float(k[3]))
    return float(hi[2]), int(hi[0]), float(lo[3]), int(lo[0])


def karsilastir(df):
    """Her detayı medyandan ikiye böler, yüksek yarı mı düşük yarı mı daha başarılı gösterir."""
    d = df.copy()
    d["abs_net"] = d["net_para_usdt"].abs()
    d["abs_sma"] = d["sma_fark_pct"].abs()
    d["abs_hareket"] = d["hareket_pct"].abs()
    satirlar = []
    for col, ad in [("kat", "Normalin katı"), ("oi_pct", "OI artışı"),
                    ("abs_sma", "SMA uzaklık"), ("abs_hareket", "Fiyat hareketi"),
                    ("abs_net", "Net para")]:
        med = d[col].median()
        y, a = d[d[col] >= med], d[d[col] < med]
        if len(y) == 0 or len(a) == 0:
            continue
        satirlar.append(
            f"• {ad}: yüksek yarı %{(y['yone_gore_pct'] > 0).mean() * 100:.0f} başarı, "
            f"ort max %{y['max_lehine_pct'].mean():.2f} | düşük yarı "
            f"%{(a['yone_gore_pct'] > 0).mean() * 100:.0f} başarı, "
            f"ort max %{a['max_lehine_pct'].mean():.2f}")
    return satirlar


def gun_sonu_raporu():
    global son_rapor_tarihi
    simdi = datetime.now(TZ)
    gun_bas = simdi.replace(hour=0, minute=0, second=0, microsecond=0)
    bas_ms = int(gun_bas.timestamp() * 1000)
    print(f"\n[RAPOR] Hazırlanıyor ({simdi:%Y-%m-%d %H:%M})...", flush=True)

    if not os.path.exists(SIGNALS_FILE):
        tg("📊 <b>GÜN SONU RAPORU (Para Akışı)</b>\n\nBugün hiç sinyal kaydı yok.")
        return
    with csv_lock:
        df = pd.read_csv(SIGNALS_FILE)
    df = df[df["ts_ms"] >= bas_ms]
    if df.empty:
        tg("📊 <b>GÜN SONU RAPORU (Para Akışı)</b>\n\nBugün hiç sinyal gelmedi.")
        return

    t = get("/fapi/v1/ticker/24hr")
    tickers = {d["symbol"]: d for d in t} if isinstance(t, list) else {}

    satirlar = []
    for _, s in df.iterrows():
        sym, giris, long = s["sembol"], float(s["giris_fiyat"]), s["yon"] == "AL"
        tk = tickers.get(sym)
        if not tk:
            continue
        guncel = float(tk["lastPrice"])
        degisim = (guncel - giris) / giris * 100
        yone = degisim if long else -degisim

        lehine = aleyhine = tepe_dk = None
        ex = extremler(sym, s["ts_ms"])
        if ex:
            mx, mx_t, mn, mn_t = ex
            if long:
                lehine = (mx - giris) / giris * 100
                aleyhine = (mn - giris) / giris * 100
                tepe_dk = max((mx_t - s["ts_ms"]) / 60000, 0)
            else:
                lehine = (giris - mn) / giris * 100
                aleyhine = (giris - mx) / giris * 100
                tepe_dk = max((mn_t - s["ts_ms"]) / 60000, 0)

        satirlar.append({
            "tarih": simdi.strftime("%Y-%m-%d"),
            "sinyal_saati": datetime.fromtimestamp(s["ts_ms"] / 1000, TZ).strftime("%H:%M:%S"),
            "sembol": sym,
            "yon": s["yon"],
            "giris_fiyat": giris,
            "rapor_fiyati": guncel,
            "degisim_pct": round(degisim, 2),
            "yone_gore_pct": round(yone, 2),
            "max_lehine_pct": round(lehine, 2) if lehine is not None else None,
            "max_aleyhine_pct": round(aleyhine, 2) if aleyhine is not None else None,
            "tepeye_dk": round(tepe_dk) if tepe_dk is not None else None,
            "net_para_usdt": s["net_para_usdt"],
            "kat": s["kat"],
            "oi_pct": s["oi_pct"],
            "sma_fark_pct": s["sma_fark_pct"],
            "hareket_pct": s["hareket_pct"],
            "24s_degisim_pct": round(float(tk.get("priceChangePercent", 0)), 2),
            "24s_hacim_usdt": round(float(tk.get("quoteVolume", 0))),
            "sonuc": "KAZANDI" if yone > 0 else "KAYBETTI",
        })

    if not satirlar:
        tg("📊 <b>GÜN SONU RAPORU (Para Akışı)</b>\n\nFiyat verisi alınamadı.")
        return

    rapor = pd.DataFrame(satirlar).sort_values("max_lehine_pct", ascending=False)
    dosya = rapor_dosya_adi(simdi)
    rapor.to_csv(dosya, index=False, encoding="utf-8-sig")

    toplam = len(rapor)
    kazanan = int((rapor["yone_gore_pct"] > 0).sum())
    al_sayi = int((rapor["yon"] == "AL").sum())
    sat_sayi = toplam - al_sayi
    en_fazla = rapor.head(5)
    en_kotu = rapor.sort_values("yone_gore_pct").head(5)

    def sat1(r):
        return (f"• {r['sembol']} ({r['yon']}): max %{r['max_lehine_pct']:+.2f} "
                f"(şu an %{r['yone_gore_pct']:+.2f}, {r['tepeye_dk']:.0f} dk'da tepe)")

    mesaj = (
        f"📊 <b>GÜN SONU RAPORU (Para Akışı) — {simdi:%d.%m.%Y}</b>\n\n"
        f"• Toplam sinyal: {toplam} (AL: {al_sayi} | SAT: {sat_sayi})\n"
        f"• Şu an kârda: {kazanan} | Zararda: {toplam - kazanan}\n"
        f"• Başarı oranı: %{kazanan / toplam * 100:.1f}\n"
        f"• Ort. getiri (yöne göre): %{rapor['yone_gore_pct'].mean():+.2f}\n"
        f"• Ort. max lehine: %{rapor['max_lehine_pct'].mean():.2f} | "
        f"ort. max aleyhine: %{rapor['max_aleyhine_pct'].mean():.2f}\n\n"
        f"🏆 <b>En fazla yapan 5</b>\n" + "\n".join(sat1(r) for _, r in en_fazla.iterrows()) + "\n\n"
        f"💥 <b>En kötü 5</b>\n" + "\n".join(
            f"• {r['sembol']} ({r['yon']}): %{r['yone_gore_pct']:+.2f}" for _, r in en_kotu.iterrows())
    )
    if toplam >= 6:
        mesaj += "\n\n🔍 <b>Hangi detay daha tutarlı?</b>\n" + "\n".join(karsilastir(rapor))
    tg(mesaj)
    tg_file(dosya, caption=f"Detaylı rapor: {dosya}")
    print(f"[RAPOR] Tamamlandı: {dosya} ({toplam} sinyal)", flush=True)


def rapor_zamani_kontrol():
    global son_rapor_tarihi
    simdi = datetime.now(TZ)
    if (simdi.hour, simdi.minute) < (REPORT_HOUR, REPORT_MINUTE):
        return
    if son_rapor_tarihi == simdi.date():
        return
    son_rapor_tarihi = simdi.date()
    if os.path.exists(rapor_dosya_adi(simdi)):   # yeniden başlatmada tekrar gönderme
        return
    threading.Thread(target=gun_sonu_raporu, daemon=True).start()


# ─────────────── TARAMA ───────────────
def check(symbol):
    data = get("/fapi/v1/klines", {"symbol": symbol, "interval": TIMEFRAME, "limit": 200})
    if not isinstance(data, list) or len(data) < SMA_LEN + KIR_BAR + 2:
        return None
    df = pd.DataFrame(data).iloc[:-1]                 # canlı mumu at
    close = df[4].astype(float).reset_index(drop=True)
    delta = (2 * df[10].astype(float) - df[7].astype(float)).reset_index(drop=True)

    # 1) Net para: coinin kendi normaline göre kaç kat
    toplam = delta.rolling(LOOK).sum()
    net = toplam.iloc[-1]
    normal = toplam.iloc[:-LOOK].abs().mean()
    if not normal or normal <= 0:
        return None
    kat = abs(net) / normal
    hareket = (close.iloc[-1] / close.iloc[-LOOK - 1] - 1) * 100

    if net > 0 and hareket >= MIN_MOVE_PCT:
        yon = "AL"
    elif net < 0 and hareket <= -MIN_MOVE_PCT:
        yon = "SAT"
    else:
        return None
    if kat < KAT:
        return None

    # 2) SMA100: yakınlık + kırılım
    sma = close.rolling(SMA_LEN).mean()
    fark = (close.iloc[-1] / sma.iloc[-1] - 1) * 100
    if abs(fark) > MAX_SMA_PCT:
        return None
    if KIR_ZORUNLU:
        onceki_c = close.iloc[-KIR_BAR - 1:-1]
        onceki_s = sma.iloc[-KIR_BAR - 1:-1]
        if yon == "AL":
            kirdi = close.iloc[-1] > sma.iloc[-1] and (onceki_c < onceki_s).any()
        else:
            kirdi = close.iloc[-1] < sma.iloc[-1] and (onceki_c > onceki_s).any()
        if not kirdi:
            return None

    # 3) OI: yeni para giriyor mu (sadece adaylar için sorulur)
    oi = get("/futures/data/openInterestHist",
             {"symbol": symbol, "period": TIMEFRAME, "limit": LOOK + 1})
    if not isinstance(oi, list) or len(oi) < LOOK + 1:
        return None
    oi_ilk = float(oi[0]["sumOpenInterestValue"])
    oi_son = float(oi[-1]["sumOpenInterestValue"])
    if oi_ilk <= 0:
        return None
    oi_pct = (oi_son / oi_ilk - 1) * 100
    if oi_pct < MIN_OI_PCT:
        return None

    return dict(symbol=symbol, yon=yon, net=net, kat=kat, hareket=hareket,
                oi=oi_pct, fark=fark, fiyat=close.iloc[-1])


def scan():
    print(f"\n[{datetime.now(timezone.utc):%H:%M:%S}] Tarama...", flush=True)
    t = get("/fapi/v1/ticker/24hr")
    if not isinstance(t, list):
        return
    semboller = [d["symbol"] for d in t
                 if d["symbol"].endswith("USDT") and float(d.get("quoteVolume", 0)) >= MIN_VOLUME]
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        sonuclar = list(ex.map(check, semboller))
    n = 0
    for s in filter(None, sonuclar):
        key = (s["symbol"], s["yon"])
        if time.time() - son.get(key, 0) < COOLDOWN_MIN * 60:
            continue
        son[key] = time.time()
        n += 1
        log_signal(s)   # <-- rapor için kaydet
        ikon = "🟢" if s["yon"] == "AL" else "🔴"
        tg(f"{ikon} <b>PARA AKIŞI {s['yon']}</b>\n\n"
           f"• <b>Sembol</b>: {s['symbol']}\n"
           f"• <b>Net para ({LOOK} mum)</b>: {para(s['net'])} USDT\n"
           f"• <b>Normalin katı</b>: {s['kat']:.1f}x\n"
           f"• <b>Open interest</b>: %{s['oi']:+.1f}\n"
           f"• <b>SMA{SMA_LEN} uzaklık</b>: %{s['fark']:+.2f}\n"
           f"• <b>Fiyat hareketi</b>: %{s['hareket']:+.2f}\n"
           f"• <b>Fiyat</b>: {s['fiyat']}\n"
           f"• <b>TF</b>: {TIMEFRAME}")
    print(f"{len(semboller)} sembol | {n} sinyal", flush=True)


def main():
    tg("✅ Para akışı botu (SMA100 filtreli + gün sonu raporu) başladı.")
    while True:
        t0 = time.time()
        try:
            rapor_zamani_kontrol()
            scan()
        except Exception as e:
            print(f"Genel hata: {e}", flush=True)
        time.sleep(max(SCAN_SEC - (time.time() - t0), 1))


if __name__ == "__main__":
    main()
