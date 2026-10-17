import os
import time
import threading
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
import pandas as pd
import requests

# ─────────────── AYARLAR (ENV'DEN OKUNUR) ───────────────
# Kırılım (Pine "KIRILIM AL/SAT" modülü ile aynı varsayılanlar)
BREAK_LEN         = int(os.environ.get("BREAK_LEN", 20))
BO_BUFFER_PCT     = float(os.environ.get("BO_BUFFER_PCT", 0.5))
BO_VOL_MULT       = float(os.environ.get("BO_VOL_MULT", 2.0))
RSI_LEN           = int(os.environ.get("RSI_LEN", 14))
RSI_BULL          = float(os.environ.get("RSI_BULL", 60))
RSI_BEAR          = float(os.environ.get("RSI_BEAR", 40))
MIN_BARS_BETWEEN  = int(os.environ.get("MIN_BARS_BETWEEN", 5))
BODY_PCT          = float(os.environ.get("BODY_PCT", 0.0))          # istersen 5 yap: sinyal mumu min gövde %

# Teyit mumu (kırılımdan sonraki mum)
VOL_MULT          = float(os.environ.get("VOL_MULT", 1.5))
TAKER_BUY_MIN     = float(os.environ.get("TAKER_BUY_MIN", 0.55))

MIN_VOLUME_USDT   = float(os.environ.get("MIN_VOLUME_USDT", 3_000_000))
TIMEFRAME         = os.environ.get("TIMEFRAME", "5m")
SCAN_INTERVAL_SEC = int(os.environ.get("SCAN_INTERVAL_SEC", 180))   # 3 dk
MAX_WORKERS       = int(os.environ.get("MAX_WORKERS", 8))
WEIGHT_SOFT_LIMIT = int(os.environ.get("WEIGHT_SOFT_LIMIT", 1800))  # Binance limiti 2400/dk

BINANCE_FAPI      = "https://fapi.binance.com"
TELEGRAM_TOKEN    = os.environ.get("TELEGRAM_TOKEN", "BURAYA_TOKEN")
TELEGRAM_CHAT_ID  = os.environ.get("TELEGRAM_CHAT_ID", "BURAYA_CHAT_ID")

KLINES_LIMIT      = 200  # RSI/MACD ısınması için

# Geçmiş testi (python kirilim_teyit_bot.py test)
TEST_LIMIT        = int(os.environ.get("TEST_LIMIT", 1000))    # kaç mum geriye bakılsın (1000 = ~3.5 gün)
HEDEF_PCT         = float(os.environ.get("HEDEF_PCT", 10))     # hedef kâr %
UFUK_MUM          = int(os.environ.get("UFUK_MUM", 144))       # girişten sonra kaç mumda hedef aranır (144 = 12 saat)

http_session = requests.Session()
son_sinyal = {}  # {symbol: teyit mumu open_time}

block_lock = threading.Lock()
blocked_until = 0.0


def send_telegram(text):
    if TELEGRAM_TOKEN == "BURAYA_TOKEN":
        print(text, flush=True)
        return
    try:
        http_session.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            data={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"},
            timeout=10,
        )
    except Exception as e:
        print(f"Telegram hatasi: {e}", flush=True)


def set_block(seconds, neden):
    """Tüm thread'ler için ortak bekleme koyar (sadece uzatır, kısaltmaz)."""
    global blocked_until
    yeni = time.time() + seconds
    with block_lock:
        if yeni <= blocked_until:
            return
        blocked_until = yeni
    print(f"⛔ Rate limit koruması: {neden} | {int(seconds)} sn bekleniyor", flush=True)
    if neden in ("418", "429"):
        send_telegram(f"⛔ Binance {neden} aldı, bot {int(seconds)} sn duruyor.")


def block_remaining():
    return max(0.0, blocked_until - time.time())


def api_get(path, params=None):
    """Tüm Binance istekleri buradan geçer: 418/429 ve weight koruması burada."""
    if block_remaining() > 0:
        return None
    try:
        r = http_session.get(f"{BINANCE_FAPI}{path}", params=params, timeout=10)
    except Exception as e:
        print(f"İstek hatası: {e}", flush=True)
        return None

    if r.status_code in (418, 429):
        retry_after = r.headers.get("Retry-After")
        try:
            bekleme = float(retry_after)
        except (TypeError, ValueError):
            bekleme = 300 if r.status_code == 418 else 60
        set_block(bekleme + 5, str(r.status_code))
        return None

    used = r.headers.get("X-MBX-USED-WEIGHT-1M")
    if used and used.isdigit() and int(used) >= WEIGHT_SOFT_LIMIT:
        set_block(61 - (time.time() % 60), "weight")

    if r.status_code != 200:
        return None
    try:
        return r.json()
    except Exception:
        return None


def hesapla_kirilim(c):
    """Pine KIRILIM AL/SAT mantığı: her kapanmış mum için 'AL' / 'SAT' / None listesi + seviyeler."""
    close, high, low, open_, vol = c["close"], c["high"], c["low"], c["open"], c["volume"]

    # RSI (Wilder)
    delta = close.diff()
    ort_kazanc = delta.clip(lower=0).ewm(alpha=1 / RSI_LEN, adjust=False).mean()
    ort_kayip = (-delta.clip(upper=0)).ewm(alpha=1 / RSI_LEN, adjust=False).mean()
    rsi = 100 - 100 / (1 + ort_kazanc / ort_kayip)

    # MACD 12/26/9
    macd = close.ewm(span=12, adjust=False).mean() - close.ewm(span=26, adjust=False).mean()
    macd_sig = macd.ewm(span=9, adjust=False).mean()
    hist = macd - macd_sig

    # Seviyeler: önceki BREAK_LEN mumun tepesi / dibi (Pine'daki [1])
    direnc = high.rolling(BREAK_LEN).max().shift(1)
    destek = low.rolling(BREAK_LEN).min().shift(1)
    vol_sma = vol.rolling(20).mean()

    vol_ok = vol > vol_sma * BO_VOL_MULT
    body_ok = (close - open_).abs() / open_ * 100 >= BODY_PCT

    yukari = (close > direnc * (1 + BO_BUFFER_PCT / 100)) & (close.shift(1) <= direnc)
    asagi = (close < destek * (1 - BO_BUFFER_PCT / 100)) & (close.shift(1) >= destek)

    ham_al = yukari & vol_ok & body_ok & (rsi > RSI_BULL) & (hist > 0) & (macd > macd_sig)
    ham_sat = asagi & vol_ok & body_ok & (rsi < RSI_BEAR) & (hist < 0) & (macd < macd_sig)

    # Spam önleme: iki sinyal arası min MIN_BARS_BETWEEN mum
    al_l, sat_l = ham_al.tolist(), ham_sat.tolist()
    yon = [None] * len(c)
    son = None
    for i in range(len(c)):
        if al_l[i] or sat_l[i]:
            if son is None or i - son >= MIN_BARS_BETWEEN:
                yon[i] = "AL" if al_l[i] else "SAT"
                son = i
    return yon, direnc, destek


def klines_al(symbol, limit=KLINES_LIMIT):
    data = api_get("/fapi/v1/klines",
                   {"symbol": symbol, "interval": TIMEFRAME, "limit": limit})
    if not isinstance(data, list) or len(data) < 80:
        return None
    df = pd.DataFrame(data, columns=[
        "open_time", "open", "high", "low", "close", "volume",
        "close_time", "quote_volume", "trades",
        "taker_buy_base", "taker_buy_quote", "ignore"
    ])
    for col in ["open", "high", "low", "close", "volume", "taker_buy_base"]:
        df[col] = df[col].astype(float)
    # Son mum canlı -> at. Sadece KAPANMIŞ mumlarla çalış.
    return df.iloc[:-1].reset_index(drop=True)


def degerlendir(c, yon_listesi, direnc, destek, a):
    """a = kırılım mumunun indeksi, teyit mumu a+1. Dönüş: (aşama, sonuç)."""
    yon = yon_listesi[a]
    if yon is None:
        return "yok", None

    A, B = c.iloc[a], c.iloc[a + 1]
    seviye = direnc.iloc[a] if yon == "AL" else destek.iloc[a]

    if (yon == "AL" and not B["close"] > seviye) or (yon == "SAT" and not B["close"] < seviye):
        return "seviye_x", None   # teyit mumu seviyenin yanlış tarafında kapandı

    baz_hacim = c["volume"].iloc[a - 20:a].mean()  # kırılım mumundan önceki 20 mum
    if baz_hacim <= 0 or B["volume"] <= 0:
        return "hacim_x", None
    hacim_x = B["volume"] / baz_hacim
    if hacim_x < VOL_MULT:
        return "hacim_x", None

    taker_buy = B["taker_buy_base"] / B["volume"]
    if (yon == "AL" and taker_buy < TAKER_BUY_MIN) or (yon == "SAT" and taker_buy > 1 - TAKER_BUY_MIN):
        return "taker_x", None

    return "ok", {
        "yon": yon,
        "open_time": int(B["open_time"]),
        "body_pct": abs(A["close"] - A["open"]) / A["open"] * 100,
        "hacim_x": hacim_x,
        "taker_buy": taker_buy,
        "close": B["close"],
        "seviye": seviye,
        "uzaklik": (B["close"] - seviye) / seviye * 100,
    }


sayac = {"taranan": 0, "kirilim": 0, "seviye_x": 0, "hacim_x": 0, "taker_x": 0, "ok": 0}
sayac_lock = threading.Lock()


def check_symbol(symbol):
    c = klines_al(symbol)
    if c is None:
        return None
    yon_l, direnc, destek = hesapla_kirilim(c)
    asama, sonuc = degerlendir(c, yon_l, direnc, destek, len(c) - 2)

    with sayac_lock:
        sayac["taranan"] += 1
        if asama != "yok":
            sayac["kirilim"] += 1
            sayac[asama] += 1
    if asama != "yok":
        print(f"  [kırılım] {symbol} {yon_l[-2]} -> {asama}", flush=True)
    if sonuc:
        sonuc["symbol"] = symbol
    return sonuc


def ileri_sonuc(c, b_idx, yon, giris):
    """Girişten (teyit mumu kapanışı) sonraki UFUK_MUM mumda hedefe ulaşıldı mı?"""
    ileri = c.iloc[b_idx + 1: b_idx + 1 + UFUK_MUM]
    if len(ileri) == 0:
        return None
    if yon == "AL":
        hedef = giris * (1 + HEDEF_PCT / 100)
        vurdu = (ileri["high"] >= hedef).tolist()
        maks_kar = ileri["high"].max() / giris - 1
    else:
        hedef = giris * (1 - HEDEF_PCT / 100)
        vurdu = (ileri["low"] <= hedef).tolist()
        maks_kar = 1 - ileri["low"].min() / giris
    if True in vurdu:
        k = vurdu.index(True)
        once = ileri.iloc[:k]
        sure_mum = k + 1
    else:
        once, sure_mum = ileri, None
    if len(once) > 0:
        maks_dusus = (1 - once["low"].min() / giris) if yon == "AL" else (once["high"].max() / giris - 1)
    else:
        maks_dusus = 0.0
    return {
        "hedef_vurdu": sure_mum is not None,
        "sure_dk": sure_mum * 5 if sure_mum else None,
        "maks_kar_pct": maks_kar * 100,
        "once_maks_dusus_pct": max(maks_dusus, 0) * 100,
        "ileri_mum": len(ileri),
    }


def gecmis_test():
    """python kirilim_teyit_bot.py test -> geçmiş sinyalleri sayar ve %HEDEF_PCT kâra ulaşanları listeler."""
    tickers = api_get("/fapi/v1/ticker/24hr")
    if not isinstance(tickers, list):
        print("Ticker alınamadı.")
        return
    semboller = [d["symbol"] for d in tickers
                 if d["symbol"].endswith("USDT") and float(d.get("quoteVolume", 0)) >= MIN_VOLUME_USDT]
    toplam = {"yok": 0, "seviye_x": 0, "hacim_x": 0, "taker_x": 0, "ok": 0}

    def isle(sym):
        c = klines_al(sym, TEST_LIMIT)
        if c is None:
            return None
        yon_l, direnc, destek = hesapla_kirilim(c)
        yerel = dict.fromkeys(toplam, 0)
        liste = []
        for a in range(30, len(c) - 1):
            asama, sonuc = degerlendir(c, yon_l, direnc, destek, a)
            yerel[asama] += 1
            if sonuc:
                sonuc["symbol"] = sym
                sonuc["zaman_utc"] = datetime.fromtimestamp(sonuc["open_time"] / 1000 + 300, timezone.utc).strftime("%m-%d %H:%M")
                sonuc.update(ileri_sonuc(c, a + 1, sonuc["yon"], sonuc["close"]) or
                             {"hedef_vurdu": False, "sure_dk": None, "maks_kar_pct": 0,
                              "once_maks_dusus_pct": 0, "ileri_mum": 0})
                liste.append(sonuc)
        return yerel, liste

    sinyaller = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        for r in ex.map(isle, semboller):
            if r:
                for k, v in r[0].items():
                    toplam[k] += v
                sinyaller.extend(r[1])

    kirilim = sum(v for k, v in toplam.items() if k != "yok")
    saat = TEST_LIMIT * 5 / 60
    print(f"\n{len(semboller)} sembol, yaklaşık son {saat:.0f} saat:")
    print(f"  Kırılım mumu (A) sayısı : {kirilim}")
    print(f"  Teyitte elenen          : seviye {toplam['seviye_x']} | hacim {toplam['hacim_x']} | taker {toplam['taker_x']}")
    print(f"  Teyitli sinyal          : {toplam['ok']}")

    if not sinyaller:
        return
    df = pd.DataFrame(sinyaller).sort_values("open_time")
    df.to_csv("sinyal_listesi.csv", index=False)

    tam = df[df["ileri_mum"] >= UFUK_MUM]   # tüm ufku görebilenler
    eksik = len(df) - len(tam)
    vuran = tam[tam["hedef_vurdu"]]
    print(f"\n── %{HEDEF_PCT:g} hedef testi (giriş: teyit mumu kapanışı, ufuk: {UFUK_MUM * 5 // 60} saat) ──")
    print(f"  Değerlendirilen sinyal : {len(tam)}  (ufku dolmayan {eksik} sinyal hariç)")
    if len(tam):
        print(f"  %{HEDEF_PCT:g}'e ulaşan         : {len(vuran)}  (%{len(vuran) / len(tam) * 100:.0f})")
        print(f"  Ortalama maks. kâr     : %{tam['maks_kar_pct'].mean():.1f}")
        print(f"  Hedefe giderken ortalama önce düşüş: %{vuran['once_maks_dusus_pct'].mean() if len(vuran) else 0:.1f}")
    if len(vuran):
        print("\n  ✅ Hedefe ulaşanlar:")
        for _, r in vuran.iterrows():
            print(f"   {r['zaman_utc']} UTC  {r['symbol']:<14} {r['yon']:<3} "
                  f"{int(r['sure_dk'])} dk'da | maks %{r['maks_kar_pct']:.1f} | önce düşüş %{r['once_maks_dusus_pct']:.1f}")
    print("\n  Tüm sinyaller: sinyal_listesi.csv dosyasına yazıldı.")


def scan_once():
    print(f"\n[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] Tarama başladı...", flush=True)

    tickers = api_get("/fapi/v1/ticker/24hr")
    if not isinstance(tickers, list):
        print("Ticker alınamadı, tarama atlandı.", flush=True)
        return

    semboller = [
        d["symbol"] for d in tickers
        if d["symbol"].endswith("USDT") and float(d.get("quoteVolume", 0)) >= MIN_VOLUME_USDT
    ]

    bulunan = 0
    for k in sayac:
        sayac[k] = 0
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futures = {ex.submit(check_symbol, s): s for s in semboller}
        for f in as_completed(futures):
            symbol = futures[f]
            try:
                s = f.result()
            except Exception as e:
                print(f"{symbol} hata: {e}", flush=True)
                continue
            if not s:
                continue
            if son_sinyal.get(symbol) == s["open_time"]:
                continue
            son_sinyal[symbol] = s["open_time"]
            bulunan += 1

            ikon = "🟢" if s["yon"] == "AL" else "🔴"
            send_telegram(
                f"{ikon} <b>KIRILIM TEYİTLİ {s['yon']}</b>\n\n"
                f"• <b>Sembol</b>: {s['symbol']}\n"
                f"• <b>Kırılım mumu gövde</b>: %{s['body_pct']:.2f}\n"
                f"• <b>Kırılan seviye ({BREAK_LEN} mum)</b>: {s['seviye']}\n"
                f"• <b>Teyit hacmi</b>: {s['hacim_x']:.1f}x\n"
                f"• <b>Alıcı oranı</b>: %{s['taker_buy'] * 100:.0f}\n"
                f"• <b>Fiyat</b>: {s['close']} (seviyeden %{s['uzaklik']:+.2f})\n"
                f"• <b>Zaman Dilimi</b>: {TIMEFRAME}"
            )
            print(f"[SİNYAL] {s['symbol']} {s['yon']}", flush=True)

    print(f"Tarama bitti. Taranan {sayac['taranan']}/{len(semboller)} | kırılım {sayac['kirilim']} | "
          f"elenen: seviye {sayac['seviye_x']}, hacim {sayac['hacim_x']}, taker {sayac['taker_x']} | "
          f"{bulunan} yeni sinyal.", flush=True)


def main():
    if len(__import__("sys").argv) > 1 and __import__("sys").argv[1] == "test":
        gecmis_test()
        return
    print(f"Bot başlatıldı | TF={TIMEFRAME} | Aralık={SCAN_INTERVAL_SEC}sn", flush=True)
    while True:
        baslangic = time.time()
        try:
            scan_once()
        except Exception as e:
            print(f"Genel hata: {e}", flush=True)
        bekle = max(SCAN_INTERVAL_SEC - (time.time() - baslangic), block_remaining(), 1)
        time.sleep(bekle)


if __name__ == "__main__":
    main()
