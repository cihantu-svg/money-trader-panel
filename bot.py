"""
Basit Tarayıcı Bot - SADECE 3 KRİTER:
1) 15 dakikalık mum
2) GÜNLÜK (24 saatlik) hacim >= 3,000,000 USDT
3) Son 15dk fiyat değişimi >= %7 (mutlak değer, yani hem yükseliş hem düşüş)

Başka HİÇBİR filtre yok (RSI, SMA, trend, breakout vs. YOK).
Binance Futures üzerinde tüm USDT paritelerini tarar.
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
PRICE_CHANGE_MIN = float(os.getenv("PRICE_CHANGE_MIN", "7.0"))     # minimum %7 hareket (15dk mumda)
CHECK_INTERVAL_SEC = int(os.getenv("SCAN_INTERVAL_SEC", os.getenv("CHECK_INTERVAL_SEC", "60")))    # kaç saniyede bir tarasın
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
MAX_RETRIES = 3
RETRY_BACKOFF_BASE = 0.5
# ==================================

BINANCE_FAPI = "https://fapi.binance.com"

session = requests.Session()


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


def get_last_15m_kline(symbol: str):
    """Kapanmış son 15 dakikalık mumu döndürür."""
    try:
        r = _get_with_retry(
            f"{BINANCE_FAPI}/fapi/v1/klines",
            params={"symbol": symbol, "interval": "15m", "limit": 2},
            timeout=10,
        )
    except Exception as e:
        log.debug(f"{symbol} kline hata: {e}")
        return None
    if len(r) < 2:
        return None
    # r[-1] genelde henüz kapanmamış olabilir, kapanmış olan bir öncekini alıyoruz
    kline = r[-2]
    open_price = float(kline[1])
    high_price = float(kline[2])
    low_price = float(kline[3])
    close_price = float(kline[4])
    volume_base = float(kline[5])
    quote_volume = float(kline[7])  # USDT cinsinden hacim
    return {
        "open": open_price,
        "high": high_price,
        "low": low_price,
        "close": close_price,
        "quote_volume": quote_volume,
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

    for symbol in symbols:
        try:
            daily_vol = daily_volumes.get(symbol, 0.0)
            if daily_vol < VOLUME_USDT_MIN:
                continue  # günlük hacim yetersizse mum verisine bile bakma

            k = get_last_15m_kline(symbol)
            if k is None:
                continue

            change_pct = (k["close"] - k["open"]) / k["open"] * 100

            if abs(change_pct) < PRICE_CHANGE_MIN:
                continue

            hits.append({
                "symbol": symbol,
                "change_pct": change_pct,
                "volume_usdt_24h": daily_vol,
                "close": k["close"],
            })
        except Exception as e:
            log.debug(f"{symbol} hata: {e}")
        time.sleep(0.05)  # rate limit için küçük bekleme

    return hits


def format_alert(hit: dict) -> str:
    direction = "🟢 YÜKSELİŞ" if hit["change_pct"] > 0 else "🔴 DÜŞÜŞ"
    return (
        f"{direction} | {hit['symbol']}\n"
        f"15dk Değişim: {hit['change_pct']:.2f}%\n"
        f"Günlük Hacim: ${hit['volume_usdt_24h']:,.0f}\n"
        f"Fiyat: {hit['close']}\n"
        f"Zaman: {datetime.now(timezone.utc).strftime('%H:%M:%S UTC')}"
    )


def main():
    _debug_env_check()
    log.info("Tarayıcı başladı. Kriterler: 15dk mum | Günlük Hacim >= $%s | 15dk Değişim >= %%%s"
              % (f"{VOLUME_USDT_MIN:,.0f}", PRICE_CHANGE_MIN))
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log.error("TELEGRAM_TOKEN / TELEGRAM_CHAT_ID env variable olarak tanımlı değil! "
                  "Sunucu panelinden ekle, yoksa Telegram mesajı gitmez.")
    send_telegram(
        "✅ Tarayıcı başladı.\n"
        f"Kriterler: Günlük Hacim >= ${VOLUME_USDT_MIN:,.0f} | 15dk Değişim >= %{PRICE_CHANGE_MIN}\n"
        "Herhangi bir değişiklik yok, sadece bu 2 kriter aktif."
    )
    already_alerted = set()

    while True:
        try:
            hits = scan_once()
            for hit in hits:
                key = (hit["symbol"], round(hit["change_pct"], 1))
                if key in already_alerted:
                    continue
                already_alerted.add(key)

                msg = format_alert(hit)
                log.info(msg)
                send_telegram(msg)

            # aynı mumda tekrar alarm atmasın diye set'i belirli aralıkla temizle
            if len(already_alerted) > 500:
                already_alerted.clear()

        except Exception as e:
            log.error(f"Tarama döngüsü hatası: {e}")

        time.sleep(CHECK_INTERVAL_SEC)


if __name__ == "__main__":
    main()
