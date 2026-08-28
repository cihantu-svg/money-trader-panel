"""
Basit Tarayıcı Bot - SADECE 3 KRİTER:
1) 15 dakikalık mum
2) GÜNLÜK (24 saatlik) hacim >= 3,000,000 USDT
3) AÇIK (henüz kapanmamış) 15dk mumun o ana kadarki değişimi >= %7
   (mutlak değer, yani hem yükseliş hem düşüş)

Başka HİÇBİR filtre yok (RSI, SMA, trend, breakout vs. YOK).
Binance Futures üzerinde tüm USDT paritelerini tarar.

────────────────────────────────────────────────────────────────────
DUZELTME: eskiden SADECE KAPANMIŞ mum kontrol ediliyordu (r[-2]).
Bu, fiyatın mum İÇİNDE %7'yi geçip mum kapanmadan geri çekildiği
durumları TAMAMEN KAÇIRIYORDU - kapanışta %7'nin altında kaldığı
için hiç sinyal gelmiyordu. Şimdi HENÜZ OLUŞMAKTA OLAN mum (r[-1])
kontrol ediliyor; eşik ilk geçildiği anda, mum kapanmasını
beklemeden sinyal geliyor.

Ayrıca artık coinler PARALEL (ThreadPoolExecutor) taranıyor - eskiden
tek tek sıralı tarama ~400 coin için tek başına 30-60 saniye
sürüyordu, bu da "canlı" sinyal için çok yavaştı.
────────────────────────────────────────────────────────────────────
"""

import os
import time
import logging
import requests
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger(__name__)

# ============ AYARLAR ============
# Token'lar artik ortam degiskeninden (environment variable) okunuyor,
# koda hardcode edilmiyor - sunucu panelinde TELEGRAM_TOKEN ve
# TELEGRAM_CHAT_ID olarak tanimla.
VOLUME_USDT_MIN = float(os.getenv("VOLUME_USDT_MIN", "3000000"))   # minimum GÜNLÜK (24s) hacim ($)
PRICE_CHANGE_MIN = float(os.getenv("PRICE_CHANGE_MIN", "7.0"))     # minimum %7 hareket (açık mumda)
CHECK_INTERVAL_SEC = int(os.getenv("SCAN_INTERVAL_SEC", os.getenv("CHECK_INTERVAL_SEC", "10")))    # kaç saniyede bir tarasın (acik mum icin sik taranmali)
MAX_WORKERS = int(os.getenv("MAX_WORKERS", "15"))   # paralel coin tarama sayisi
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
MAX_RETRIES = 3
RETRY_BACKOFF_BASE = 0.5
# ==================================

BINANCE_FAPI = "https://fapi.binance.com"

session = requests.Session()
_adapter = requests.adapters.HTTPAdapter(pool_connections=MAX_WORKERS + 5, pool_maxsize=MAX_WORKERS + 10)
session.mount("https://", _adapter)


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


def get_current_15m_candle(symbol: str):
    """HENUZ KAPANMAMIS (su an olusmakta olan) 15 dakikalik mumu döndürür.
    Eskiden r[-2] (kapanmis onceki mum) kullaniliyordu - bu, fiyatin mum
    icinde esigi gecip mum kapanmadan geri cekildigi durumlari kaciriyordu.
    Simdi r[-1] (acik/canli mum) kullaniliyor."""
    try:
        r = _get_with_retry(
            f"{BINANCE_FAPI}/fapi/v1/klines",
            params={"symbol": symbol, "interval": "15m", "limit": 1},
            timeout=10,
        )
    except Exception as e:
        log.debug(f"{symbol} kline hata: {e}")
        return None
    if not r or len(r) < 1:
        return None
    kline = r[-1]  # su an acik olan mum
    open_time = int(kline[0])
    open_price = float(kline[1])
    high_price = float(kline[2])
    low_price = float(kline[3])
    close_price = float(kline[4])  # "close" alani, kapanmamis mumda GUNCEL fiyattir
    quote_volume = float(kline[7])
    return {
        "open_time": open_time,
        "open": open_price,
        "high": high_price,
        "low": low_price,
        "close": close_price,
        "quote_volume": quote_volume,
    }


def _check_symbol(symbol, daily_vol):
    """Tek bir coin icin: acik mumu ceker, esigi kontrol eder."""
    k = get_current_15m_candle(symbol)
    if k is None:
        return None

    change_pct = (k["close"] - k["open"]) / k["open"] * 100
    if abs(change_pct) < PRICE_CHANGE_MIN:
        return None

    return {
        "symbol": symbol,
        "window_open_time": k["open_time"],
        "change_pct": change_pct,
        "volume_usdt_24h": daily_vol,
        "close": k["close"],
    }


def scan_once():
    hits = []
    symbols = get_usdt_futures_symbols()
    if not symbols:
        log.error("Sembol listesi BOS - bu turu atlıyorum")
        return hits

    daily_volumes = get_24h_volumes()  # tek seferde tüm sembollerin günlük hacmi
    if daily_volumes is None:
        # Hacim verisi çekilemedi - tüm coinleri elemek yerine bu turda
        # hacim filtresini atla, tüm semboller taransın.
        log.error(f"24h hacim verisi alınamadı - bu turda hacim filtresi ATLANIYOR, "
                  f"tüm {len(symbols)} coin filtrelenmeden taranacak")
        daily_volumes = {s: VOLUME_USDT_MIN for s in symbols}  # hepsi eşiği geçmiş sayılır

    candidates = [s for s in symbols if daily_volumes.get(s, 0.0) >= VOLUME_USDT_MIN]

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(_check_symbol, s, daily_volumes.get(s, 0.0)): s for s in candidates}
        for future in as_completed(futures):
            try:
                result = future.result()
                if result is not None:
                    hits.append(result)
            except Exception as e:
                log.debug(f"{futures[future]} hata: {e}")

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
        "✅ Tarayıcı başladı (AÇIK MUM modu).\n"
        f"Kriterler: Günlük Hacim >= ${VOLUME_USDT_MIN:,.0f} | Değişim >= %{PRICE_CHANGE_MIN}\n"
        f"Mum kapanmasını beklemeden, eşik geçilir geçilmez sinyal gelir."
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
