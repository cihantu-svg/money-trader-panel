"""
Basit Tarayıcı Bot - SADECE 3 KRİTER:
1) 15 dakikalık mum
2) GÜNLÜK (24 saatlik) hacim >= 3,000,000 USDT
3) AÇIK (henüz kapanmamış) 15dk mumun o ana kadarki değişimi >= %7
   (mutlak değer, yani hem yükseliş hem düşüş)

Başka HİÇBİR filtre yok (RSI, SMA, trend, breakout vs. YOK).
Binance Futures üzerinde tüm USDT paritelerini tarar.

────────────────────────────────────────────────────────────────────
DUZELTME 1 (onceki): eskiden SADECE KAPANMIŞ mum kontrol ediliyordu.
Şimdi HENÜZ OLUŞMAKTA OLAN mum (açık mum) kontrol ediliyor.

DUZELTME 2 (BU SURUM) - "bazı coinler kaçıyor" sorunu:
İki gercek sebep bulundu:

  a) Hata loglari log.debug() ile yaziliyordu ama logger INFO
     seviyesinde kuruluydu - yani basarisiz coin istekleri HİÇ
     GORUNMUYORDU. Simdi log.warning() kullaniliyor ve her turda
     kac coin'in basarisiz oldugu ozetle raporlaniyor.

  b) ASIL SEBEP: eskiden her coin icin AYRI bir /fapi/v1/klines
     istegi atiliyordu, 10 saniyede bir, ~400 coin icin. Bu dakikada
     2400+ istek demekti - Binance Futures agirlik limitine (dakikada
     ~2400) TAM SINIRDA/USTUNDE. Limit asilinca 429/418 donuyor,
     retry'ler tukenince o coin sessizce (yukaridaki a sebebiyle
     GORUNMEDEN) atlaniyordu. "Bazi coinlerin es gecilmesi" tam olarak
     boyle rastgele/araliksiz bir desenle ortaya cikar.

     COZUM: Artik her coin icin ayri istek YOK. Binance'in TEK istekte
     TUM sembollerin GUNCEL fiyatini donduren /fapi/v1/ticker/price
     endpoint'i kullaniliyor (tek cagri, tum coinler). Her 15dk
     penceresinin "acilis fiyati", o pencerede ilk gorulen bulk fiyat
     olarak yerelde (hafizada) saklaniyor - boylece degisim yuzdesi
     sonraki her turda ekstra istek atmadan hesaplanabiliyor.

     ONEMLI VARSAYIM: pencere "acilisi" olarak GERCEK 15dk mum
     acilisi degil, o pencerede ILK GOZLEMLENEN bulk fiyat kullanilir
     (en fazla CHECK_INTERVAL_SEC kadar gecikmeli olabilir - orn. 5sn
     tarama araliginda en fazla 5sn'lik sapma). %7 gibi buyuk bir
     esik icin bu sapma pratikte onemsizdir, ama bilinmesi gereken bir
     yaklastirmadir.
────────────────────────────────────────────────────────────────────
"""

import os
import time
import logging
import requests
from datetime import datetime, timezone

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger(__name__)

# ============ AYARLAR ============
# Token'lar artik ortam degiskeninden (environment variable) okunuyor,
# koda hardcode edilmiyor - sunucu panelinde TELEGRAM_TOKEN ve
# TELEGRAM_CHAT_ID olarak tanimla.
VOLUME_USDT_MIN = float(os.getenv("VOLUME_USDT_MIN", "3000000"))   # minimum GÜNLÜK (24s) hacim ($)
PRICE_CHANGE_MIN = float(os.getenv("PRICE_CHANGE_MIN", "7.0"))     # minimum %7 hareket (açık mumda)
CHECK_INTERVAL_SEC = int(os.getenv("SCAN_INTERVAL_SEC", os.getenv("CHECK_INTERVAL_SEC", "5")))    # kaç saniyede bir tarasın
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
MAX_RETRIES = 3
RETRY_BACKOFF_BASE = 0.5
WINDOW_MS = 15 * 60 * 1000
# ==================================

BINANCE_FAPI = "https://fapi.binance.com"

session = requests.Session()
_adapter = requests.adapters.HTTPAdapter(pool_connections=10, pool_maxsize=10)
session.mount("https://", _adapter)

# symbol -> (window_start_ms, o pencerede ilk gozlemlenen fiyat)
# artik her coin icin ayri istek atilmadigindan, "acilis" fiyati burada
# yerelde takip edilir.
_window_open_price = {}


def _debug_env_check():
    """Render'in gercekte hangi isimle ne enjekte ettigini gormek icin
    tanı amaçlı log - deger sizdirmadan sadece degisken adlarini ve
    uzunluklarini gosterir. Sorun cozulunce bu fonksiyon kaldirilabilir."""
    all_keys = sorted(os.environ.keys())
    telegram_related = [k for k in all_keys if "TELEGRAM" in k.upper()]
    log.info(f"[TANI] Ortamda TELEGRAM icin gecen degisken adlari: {telegram_related}")
    log.info(f"[TANI] TELEGRAM_TOKEN uzunlugu: {len(TELEGRAM_TOKEN)} karakter "
              f"(0 ise degisken bos ya da hic yok)")
    log.info(f"[TANI] TELEGRAM_CHAT_ID uzunlugu: {len(TELEGRAM_CHAT_ID)} karakter")
    if not telegram_related:
        log.error("[TANI] Ortamda 'TELEGRAM' geçen HİÇBİR değişken bulunamadı. "
                  "Bu, değişkenlerin bu servise hiç ulaşmadığını gösterir - "
                  "yanlış servise eklenmiş, farklı bir Environment Group'a "
                  "bağlanmış, ya da isim/yazım hatası olabilir.")


def _get_with_retry(url, params=None, timeout=15):
    """Gecici ag/API hatalarinda (429/5xx/timeout) otomatik tekrar dener,
    tek seferlik hatalarda taramanin tamamen bos donmesini engeller."""
    last_exc = None
    for attempt in range(MAX_RETRIES + 1):
        try:
            r = session.get(url, params=params, timeout=timeout)
            if r.status_code == 200:
                return r.json()
            wait = RETRY_BACKOFF_BASE * (2 ** attempt)
            log.warning(f"HTTP {r.status_code} ({url}), {wait:.1f}sn sonra tekrar (deneme {attempt + 1})")
            time.sleep(wait)
            last_exc = Exception(f"HTTP {r.status_code}")
        except requests.exceptions.RequestException as e:
            last_exc = e
            wait = RETRY_BACKOFF_BASE * (2 ** attempt)
            time.sleep(wait)
    raise last_exc if last_exc else Exception("Bilinmeyen istek hatasi")


def send_telegram(msg: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log.warning("[TELEGRAM DEVRE DIŞI - TOKEN/CHAT_ID env variable olarak tanimli degil] " + msg)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        r = session.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": msg}, timeout=10)
        if r.status_code != 200:
            log.error(f"Telegram gönderim hatası: HTTP {r.status_code} - {r.text[:200]}")
    except Exception as e:
        log.error(f"Telegram gönderim hatası: {e}")


def get_usdt_futures_symbols():
    try:
        data = _get_with_retry(f"{BINANCE_FAPI}/fapi/v1/exchangeInfo", timeout=15)
        symbols = [
            s["symbol"] for s in data["symbols"]
            if s["symbol"].endswith("USDT") and s.get("contractType") == "PERPETUAL"
            and s.get("status") == "TRADING"
        ]
        if not symbols:
            log.error("exchangeInfo bos sembol listesi döndürdü")
        return symbols
    except Exception as e:
        log.error(f"get_usdt_futures_symbols hata (sembol listesi ALINAMADI): {e}")
        return []


def get_24h_volumes():
    """Tüm semboller için 24 saatlik quote volume (USDT) sözlüğü döndürür.
    Hata durumunda None döner (bos sözlükten AYIRT edilmesi kritik - yoksa
    hacim verisi çekilemediginde tüm coinler hacim=0 sanılıp elenir)."""
    try:
        data = _get_with_retry(f"{BINANCE_FAPI}/fapi/v1/ticker/24hr", timeout=15)
        vols = {item["symbol"]: float(item["quoteVolume"]) for item in data}
        if not vols:
            log.error("24hr ticker API bos veri döndürdü")
            return None
        return vols
    except Exception as e:
        log.error(f"get_24h_volumes hata (24h hacim verisi ALINAMADI): {e}")
        return None


def get_all_prices():
    """TUM sembollerin GUNCEL fiyatini TEK istekte doner (bulk).
    Kritik: boylece her coin icin ayri ayri kline istegi atmaya gerek
    kalmiyor - hem cok daha hizli hem de rate-limit riskini ortadan
    kaldiriyor. Hata durumunda None doner."""
    try:
        data = _get_with_retry(f"{BINANCE_FAPI}/fapi/v1/ticker/price", timeout=15)
        prices = {item["symbol"]: float(item["price"]) for item in data}
        if not prices:
            log.error("ticker/price API bos veri döndürdü")
            return None
        return prices
    except Exception as e:
        log.error(f"get_all_prices hata (fiyat verisi ALINAMADI): {e}")
        return None


def scan_once():
    hits = []
    symbols = get_usdt_futures_symbols()
    if not symbols:
        log.error("Sembol listesi BOS - bu turu atlıyorum")
        return hits

    daily_volumes = get_24h_volumes()  # tek seferde tüm sembollerin günlük hacmi
    if daily_volumes is None:
        log.error(f"24h hacim verisi alınamadı - bu turda hacim filtresi ATLANIYOR, "
                  f"tüm {len(symbols)} coin filtrelenmeden taranacak")
        daily_volumes = {s: VOLUME_USDT_MIN for s in symbols}

    candidates = set(s for s in symbols if daily_volumes.get(s, 0.0) >= VOLUME_USDT_MIN)

    prices = get_all_prices()
    if prices is None:
        log.error("Fiyat verisi alınamadı (ticker/price) - bu turu atlıyorum")
        return hits

    now_ms = int(time.time() * 1000)
    current_window_start = (now_ms // WINDOW_MS) * WINDOW_MS

    missing_price = 0
    for symbol in candidates:
        price = prices.get(symbol)
        if price is None:
            missing_price += 1
            continue

        prev = _window_open_price.get(symbol)
        if prev is None or prev[0] != current_window_start:
            # yeni pencere basladi (ya da bu coini ilk kez goruyoruz) -
            # acilis fiyati olarak bu ilk gozlemi kaydet, bu turda
            # henuz karsilastirma yapilmaz
            _window_open_price[symbol] = (current_window_start, price)
            continue

        open_price = prev[1]
        if open_price == 0:
            continue
        change_pct = (price - open_price) / open_price * 100
        if abs(change_pct) < PRICE_CHANGE_MIN:
            continue

        hits.append({
            "symbol": symbol,
            "window_open_time": current_window_start,
            "change_pct": change_pct,
            "volume_usdt_24h": daily_volumes.get(symbol, 0.0),
            "close": price,
        })

    if missing_price:
        log.warning(f"{missing_price} coin icin fiyat verisi bulunamadi (ticker/price listesinde yoktu)")

    # eski pencerelere ait kayitlari temizle (bellek sismesin diye) -
    # 2+ pencere (30dk+) once kalmis kayitlar artik kullanilmayacaktir
    if _window_open_price:
        very_stale = [s for s, (w, _) in _window_open_price.items() if current_window_start - w >= 2 * WINDOW_MS]
        for s in very_stale:
            del _window_open_price[s]

    return hits


def format_alert(hit: dict) -> str:
    direction = "🟢 YÜKSELİŞ" if hit["change_pct"] > 0 else "🔴 DÜŞÜŞ"
    return (
        f"{direction} | {hit['symbol']}  (AÇIK MUM - henüz kapanmadı)\n"
        f"15dk Değişim: {hit['change_pct']:.2f}%\n"
        f"Günlük Hacim: ${hit['volume_usdt_24h']:,.0f}\n"
        f"Fiyat: {hit['close']}\n"
        f"Zaman: {datetime.now(timezone.utc).strftime('%H:%M:%S UTC')}"
    )


def main():
    _debug_env_check()
    log.info("Tarayıcı başladı. Kriterler: AÇIK 15dk mum | Günlük Hacim >= $%s | Değişim >= %%%s | Tarama: %ss"
              % (f"{VOLUME_USDT_MIN:,.0f}", PRICE_CHANGE_MIN, CHECK_INTERVAL_SEC))
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log.error("TELEGRAM_TOKEN / TELEGRAM_CHAT_ID env variable olarak tanımlı değil! "
                  "Sunucu panelinden ekle, yoksa Telegram mesajı gitmez.")
    send_telegram(
        "✅ Tarayıcı başladı (AÇIK MUM + TEK-İSTEK/bulk fiyat modu).\n"
        f"Kriterler: Günlük Hacim >= ${VOLUME_USDT_MIN:,.0f} | Değişim >= %{PRICE_CHANGE_MIN}\n"
        f"Mum kapanmasını beklemeden, eşik geçilir geçilmez sinyal gelir. "
        f"Artık coin başına ayrı istek atılmıyor - rate-limit riski ortadan kalktı."
    )
    # symbol -> son sinyal verilen pencerenin open_time'i (ayni pencerede tekrar
    # alarm atmasin, ama pencere degisince yeni sinyal verebilsin diye)
    alerted_window = {}

    while True:
        try:
            hits = scan_once()
            for hit in hits:
                symbol = hit["symbol"]
                window = hit["window_open_time"]
                if alerted_window.get(symbol) == window:
                    continue  # bu pencerede zaten uyarildik
                alerted_window[symbol] = window

                msg = format_alert(hit)
                log.info(msg)
                send_telegram(msg)

        except Exception as e:
            log.error(f"Tarama döngüsü hatası: {e}")

        time.sleep(CHECK_INTERVAL_SEC)


if __name__ == "__main__":
    main()
