import os
import csv
import time
import threading
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
import pandas as pd
import requests

try:
    from zoneinfo import ZoneInfo
except Exception:
    ZoneInfo = None

# ─────────────── AYARLAR (ENV'DEN OKUNUR) ───────────────
SMA_LEN          = int(os.environ.get("SMA_LEN", 100))
BODY_PCT         = float(os.environ.get("BODY_PCT", 5.0))
MIN_VOLUME_USDT  = float(os.environ.get("MIN_VOLUME_USDT", 3_000_000))
TIMEFRAME        = os.environ.get("TIMEFRAME", "15m")

SCAN_INTERVAL_SEC = int(os.environ.get("SCAN_INTERVAL_SEC", 180))
MAX_WORKERS       = 15

# Gün sonu raporu ayarları
REPORT_HOUR       = int(os.environ.get("REPORT_HOUR", 21))     # 21 = akşam 9
REPORT_MINUTE     = int(os.environ.get("REPORT_MINUTE", 0))
REPORT_TZ         = os.environ.get("REPORT_TZ", "Europe/London")
SIGNALS_FILE      = os.environ.get("SIGNALS_FILE", "signals.csv")

BINANCE_FAPI      = "https://fapi.binance.com"
TELEGRAM_TOKEN    = os.environ.get("TELEGRAM_TOKEN", "BURAYA_TOKEN")
TELEGRAM_CHAT_ID  = os.environ.get("TELEGRAM_CHAT_ID", "BURAYA_CHAT_ID")

KLINES_LIMIT      = SMA_LEN + 10

try:
    TZ = ZoneInfo(REPORT_TZ) if ZoneInfo else timezone.utc
except Exception:
    TZ = timezone.utc

http_session = requests.Session()

ilk_sinyal_takibi = {}    # {symbol: open_time}
devam_sinyal_takibi = {}  # {symbol: set(open_time)}

rate_limit_lock = threading.Lock()
rate_limited_until = 0.0

csv_lock = threading.Lock()
son_rapor_tarihi = None


# ─────────────── TELEGRAM ───────────────
def send_telegram(text):
    if TELEGRAM_TOKEN == "BURAYA_TOKEN":
        return
    try:
        http_session.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            data={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"},
            timeout=10,
        )
    except Exception as e:
        print(f"Telegram hatasi: {e}", flush=True)


def send_telegram_file(path, caption=""):
    if TELEGRAM_TOKEN == "BURAYA_TOKEN":
        return
    try:
        with open(path, "rb") as f:
            http_session.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendDocument",
                data={"chat_id": TELEGRAM_CHAT_ID, "caption": caption},
                files={"document": f},
                timeout=30,
            )
    except Exception as e:
        print(f"Telegram dosya hatasi: {e}", flush=True)


# ─────────────── SİNYAL KAYDI (CSV) ───────────────
SIGNAL_COLS = ["ts_ms", "open_time", "sembol", "tip", "yon", "govde_pct", "giris_fiyat", "sma"]


def log_signal(sonuc):
    """Her sinyali signals.csv'ye ekler."""
    with csv_lock:
        yeni = not os.path.exists(SIGNALS_FILE)
        with open(SIGNALS_FILE, "a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if yeni:
                w.writerow(SIGNAL_COLS)
            w.writerow([
                int(time.time() * 1000),
                sonuc["open_time"],
                sonuc["symbol"],
                sonuc["tip"],
                "LONG" if "YÜKSELEN" in sonuc["direction"] else "SHORT",
                round(sonuc["body_pct"], 4),
                sonuc["close"],
                sonuc["sma100"],
            ])


# ─────────────── GÜN SONU RAPORU ───────────────
def rapor_dosya_adi(tarih):
    return f"gun_sonu_{tarih.strftime('%Y-%m-%d')}.csv"


def sinyal_sonrasi_extremler(symbol, open_time):
    """Sinyal mumundan bu yana en yüksek high ve en düşük low."""
    try:
        r = http_session.get(
            f"{BINANCE_FAPI}/fapi/v1/klines",
            params={"symbol": symbol, "interval": TIMEFRAME, "startTime": int(open_time), "limit": 1500},
            timeout=10,
        )
        data = r.json()
        if not isinstance(data, list) or not data:
            return None, None
        highs = [float(k[2]) for k in data]
        lows = [float(k[3]) for k in data]
        return max(highs), min(lows)
    except Exception:
        return None, None


def gun_sonu_raporu():
    global son_rapor_tarihi
    simdi = datetime.now(TZ)
    gun_baslangic = simdi.replace(hour=0, minute=0, second=0, microsecond=0)
    baslangic_ms = int(gun_baslangic.timestamp() * 1000)

    print(f"\n[RAPOR] Gün sonu raporu hazırlanıyor ({simdi.strftime('%Y-%m-%d %H:%M')})...", flush=True)

    if not os.path.exists(SIGNALS_FILE):
        send_telegram("📊 <b>GÜN SONU RAPORU</b>\n\nBugün hiç sinyal kaydı yok.")
        son_rapor_tarihi = simdi.date()
        return

    with csv_lock:
        df = pd.read_csv(SIGNALS_FILE)
    df = df[df["ts_ms"] >= baslangic_ms]

    if df.empty:
        send_telegram("📊 <b>GÜN SONU RAPORU</b>\n\nBugün hiç sinyal gelmedi.")
        son_rapor_tarihi = simdi.date()
        return

    # Güncel fiyat + 24s değişim + hacim (tek istekte hepsi)
    try:
        tickers = {d["symbol"]: d for d in http_session.get(f"{BINANCE_FAPI}/fapi/v1/ticker/24hr", timeout=15).json()}
    except Exception as e:
        print(f"Rapor ticker hatası: {e}", flush=True)
        tickers = {}

    satirlar = []
    for _, s in df.iterrows():
        sym = s["sembol"]
        giris = float(s["giris_fiyat"])
        long = s["yon"] == "LONG"
        t = tickers.get(sym, {})
        guncel = float(t["lastPrice"]) if "lastPrice" in t else None
        if guncel is None:
            continue

        mx, mn = sinyal_sonrasi_extremler(sym, s["open_time"])
        degisim = (guncel - giris) / giris * 100
        yone_gore = degisim if long else -degisim

        if mx is not None:
            if long:
                lehine = (mx - giris) / giris * 100
                aleyhine = (mn - giris) / giris * 100
            else:
                lehine = (giris - mn) / giris * 100
                aleyhine = (giris - mx) / giris * 100
        else:
            lehine = aleyhine = None

        yerel_zaman = datetime.fromtimestamp(s["ts_ms"] / 1000, TZ).strftime("%H:%M:%S")

        satirlar.append({
            "tarih": simdi.strftime("%Y-%m-%d"),
            "sinyal_saati": yerel_zaman,
            "sembol": sym,
            "tip": s["tip"],
            "yon": s["yon"],
            "govde_pct": round(float(s["govde_pct"]), 2),
            "giris_fiyat": giris,
            "sma": s["sma"],
            "rapor_fiyati": guncel,
            "degisim_pct": round(degisim, 2),
            "yone_gore_pct": round(yone_gore, 2),
            "max_lehine_pct": round(lehine, 2) if lehine is not None else "",
            "max_aleyhine_pct": round(aleyhine, 2) if aleyhine is not None else "",
            "24s_degisim_pct": round(float(t.get("priceChangePercent", 0)), 2),
            "24s_hacim_usdt": round(float(t.get("quoteVolume", 0))),
            "sonuc": "KAZANDI" if yone_gore > 0 else "KAYBETTI",
        })

    if not satirlar:
        send_telegram("📊 <b>GÜN SONU RAPORU</b>\n\nFiyat verisi alınamadı.")
        son_rapor_tarihi = simdi.date()
        return

    rapor = pd.DataFrame(satirlar).sort_values("yone_gore_pct", ascending=False)
    dosya = rapor_dosya_adi(simdi)
    rapor.to_csv(dosya, index=False, encoding="utf-8-sig")  # utf-8-sig: Excel'de Türkçe düzgün açılır

    # Özet
    toplam = len(rapor)
    kazanan = (rapor["yone_gore_pct"] > 0).sum()
    ilk_sayi = (rapor["tip"] == "ILK").sum()
    devam_sayi = (rapor["tip"] == "DEVAM").sum()
    ort = rapor["yone_gore_pct"].mean()
    en_iyi = rapor.head(5)
    en_kotu = rapor.tail(5).iloc[::-1]

    def satir(r):
        return f"• {r['sembol']} ({r['yon']}, {r['tip']}): %{r['yone_gore_pct']:+.2f}"

    mesaj = (
        f"📊 <b>GÜN SONU RAPORU — {simdi.strftime('%d.%m.%Y')}</b>\n\n"
        f"• Toplam sinyal: {toplam} (İlk: {ilk_sayi} | Devam: {devam_sayi})\n"
        f"• Kazanan: {kazanan} | Kaybeden: {toplam - kazanan}\n"
        f"• Başarı oranı: %{kazanan / toplam * 100:.1f}\n"
        f"• Ortalama getiri (yöne göre): %{ort:+.2f}\n\n"
        f"🏆 <b>En iyi 5</b>\n" + "\n".join(satir(r) for _, r in en_iyi.iterrows()) + "\n\n"
        f"💥 <b>En kötü 5</b>\n" + "\n".join(satir(r) for _, r in en_kotu.iterrows())
    )
    send_telegram(mesaj)
    send_telegram_file(dosya, caption=f"Detaylı rapor: {dosya}")

    son_rapor_tarihi = simdi.date()
    print(f"[RAPOR] Tamamlandı: {dosya} ({toplam} sinyal)", flush=True)


def rapor_zamani_kontrol():
    """Saat 21:00'i geçtiyse ve bugünün raporu yoksa raporu ayrı thread'de başlatır."""
    global son_rapor_tarihi
    simdi = datetime.now(TZ)
    if (simdi.hour, simdi.minute) < (REPORT_HOUR, REPORT_MINUTE):
        return
    if son_rapor_tarihi == simdi.date():
        return
    if os.path.exists(rapor_dosya_adi(simdi)):  # bot yeniden başladıysa tekrar gönderme
        son_rapor_tarihi = simdi.date()
        return
    son_rapor_tarihi = simdi.date()  # çift tetiklenmeyi önle
    threading.Thread(target=gun_sonu_raporu, daemon=True).start()


# ───────────────
