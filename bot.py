"""
İkili Tepe / İkili Dip Tarayıcı (Binance)
-----------------------------------------
Binance'te işlem gören USDT spot paritelerini 15 dakikalık grafikte tarar.

İKİLİ TEPE: Son iki pivot tepe yaklaşık aynı seviyedeyse, formasyon
yükseliş trendinde (kırılım mumu EMA üstünde) oluşmuşsa ve fiyat neckline'ı
aşağı yönlü KAPANMIŞ bir mumla kırdıysa sinyal üretir.

İKİLİ DİP: Aynısının ayna görüntüsü — formasyon düşüş trendinde (kırılım
mumu EMA altında) ise ve fiyat neckline'ı yukarı kırdıysa sinyal.

Trend filtresi sayesinde yükselişteki pullback'lar "dip", düşüşteki
tepki çıkışları "tepe" diye yanlış etiketlenmez.

Sadece KAPANMIŞ mumlarla çalışır (repaint yok).
Sinyaller hem konsola hem Telegram'a düşer.

Ortam değişkenleri:
    TELEGRAM_TOKEN       - BotFather'dan alınan bot token (zorunlu)
    TELEGRAM_CHAT_ID     - Mesajın gideceği chat id (zorunlu)
    TIMEFRAME            - zaman dilimi (varsayılan 15m)
    PIVOT_LENGTH         - pivot lookback (varsayılan 5)
    TOLERANCE_PCT        - iki tepe/dip arası max seviye farkı % (varsayılan 1.5)
    MIN_DEPTH_PCT        - tepe/dip ile neckline arası min mesafe % (varsayılan 2.0)
    NECKLINE_BUFFER_PCT  - kırılımın neckline ötesinde gitmesi gereken min mesafe % (varsayılan 0.15)
    EMA_LENGTH           - trend filtresi EMA uzunluğu (varsayılan 50)
    CONFIRM_LOOKBACK     - neckline kırılımının son kaç mum içinde sayılacağı (varsayılan 3)
    MAX_BARS_BETWEEN     - iki tepe/dip arası max mum sayısı (varsayılan 60)
    MAX_COINS            - 0 = tümü, aksi halde ilk N coin (varsayılan 0)
    SCAN_INTERVAL_SEC    - iki tarama arası bekleme, saniye (varsayılan 180)
    MAX_WORKERS          - eşzamanlı istek sayısı (varsayılan 8)
    KLINE_LIMIT          - çekilen mum sayısı (varsayılan 300)
"""

import os
import time
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("double-top-bottom-bot")

# ---------------- Ayarlar ----------------
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
TIMEFRAME = os.environ.get("TIMEFRAME", "15m")
PIVOT_LENGTH = int(os.environ.get("PIVOT_LENGTH", "5"))
TOLERANCE_PCT = float(os.environ.get("TOLERANCE_PCT", "1.5"))
MIN_DEPTH_PCT = float(os.environ.get("MIN_DEPTH_PCT", "2.0"))
NECKLINE_BUFFER_PCT = float(os.environ.get("NECKLINE_BUFFER_PCT", "0.15"))
EMA_LENGTH = int(os.environ.get("EMA_LENGTH", "50"))
CONFIRM_LOOKBACK = int(os.environ.get("CONFIRM_LOOKBACK", "3"))
MAX_BARS_BETWEEN = int(os.environ.get("MAX_BARS_BETWEEN", "60"))
MAX_COINS = int(os.environ.get("MAX_COINS", "0"))
SCAN_INTERVAL_SEC = int(os.environ.get("SCAN_INTERVAL_SEC", "180"))
MAX_WORKERS = int(os.environ.get("MAX_WORKERS", "8"))
KLINE_LIMIT = int(os.environ.get("KLINE_LIMIT", "300"))

INTERVAL_MS = {
    "1m": 60_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000,
    "1h": 3_600_000, "2h": 7_200_000, "4h": 14_400_000,
    "6h": 21_600_000, "8h": 28_800_000, "12h": 43_200_000, "1d": 86_400_000,
}.get(TIMEFRAME, 900_000)

HTTP = requests.Session()
HTTP.headers.update({"User-Agent": "double-top-bottom-bot/1.1"})

already_alerted = set()
_warned_sources = set()


def _warn_once(key, msg):
    if key not in _warned_sources:
        _warned_sources.add(key)
        log.warning(msg)


# ---------------- Veri katmanı (Binance) ----------------
def fetch_usdt_symbols():
    """Binance'te işlem gören USDT spot paritelerini döndürür."""
    try:
        r = HTTP.get("https://data-api.binance.vision/api/v3/exchangeInfo", timeout=15)
        r.raise_for_status()
        syms = r.json().get("symbols", [])
        coins = sorted({
            s["symbol"] for s in syms
            if s.get("quoteAsset") == "USDT"
            and s.get("status") == "TRADING"
            and s.get("isSpotTradingAllowed", True)
        })
        if coins:
            return coins
        log.warning("Binance boş coin listesi döndürdü.")
    except Exception as e:
        log.error("Binance coin listesi alınamadı: %s", e)
    return []


def _drop_unclosed(candles):
    """Henüz kapanmamış son mumu atar (repaint'i önlemek için)."""
    if not candles:
        return candles
    now_ms = int(time.time() * 1000)
    if candles[-1]["t"] + INTERVAL_MS > now_ms:
        return candles[:-1]
    return candles


def fetch_klines(symbol):
    """[{t,o,h,l,c}] formatında SADECE KAPANMIŞ mumlar döndürür (eskiden yeniye)."""
    try:
        r = HTTP.get(
            "https://data-api.binance.vision/api/v3/klines",
            params={"symbol": symbol, "interval": TIMEFRAME, "limit": KLINE_LIMIT},
            timeout=10,
        )
        if r.status_code != 200:
            _warn_once("bin_kline", f"Binance kline başarısız (HTTP {r.status_code} - {symbol}).")
            return None
        rows = r.json()
        if not isinstance(rows, list) or not rows:
            return None
        candles = [
            {"t": int(x[0]), "o": float(x[1]), "h": float(x[2]),
             "l": float(x[3]), "c": float(x[4])}
            for x in rows
        ]
        return _drop_unclosed(candles)
    except Exception as e:
        _warn_once("bin_kline_exc", f"Binance kline isteğinde istisna (örnek: {symbol}): {e}")
        return None


# ---------------- İkili tepe / ikili dip mantığı ----------------
def ema(values, length):
    """Üssel hareketli ortalama (tüm barlar için liste döndürür)."""
    k = 2 / (length + 1)
    out = []
    e = values[0]
    for v in values:
        e = v * k + e * (1 - k)
        out.append(e)
    return out


def pivot_highs(candles, length):
    """(index, high) listesi — sağında ve solunda 'length' mumdan yüksek olan tepe."""
    n = len(candles)
    out = []
    for i in range(length, n - length):
        h = candles[i]["h"]
        if all(h >= candles[j]["h"] for j in range(i - length, i + length + 1)):
            out.append((i, h))
    return out


def pivot_lows(candles, length):
    """(index, low) listesi — sağında ve solunda 'length' mumdan düşük olan dip."""
    n = len(candles)
    out = []
    for i in range(length, n - length):
        l = candles[i]["l"]
        if all(l <= candles[j]["l"] for j in range(i - length, i + length + 1)):
            out.append((i, l))
    return out


def detect_patterns(symbol, candles):
    """Onaylanmış ikili tepe / ikili dip sinyallerini döndürür."""
    n = len(candles)
    results = []
    ema_vals = ema([c["c"] for c in candles], EMA_LENGTH)

    # ---- İkili tepe (yükseliş trendindeki dönüş) ----
    highs = pivot_highs(candles, PIVOT_LENGTH)
    if len(highs) >= 2:
        (i1, p1), (i2, p2) = highs[-2], highs[-1]
        if 0 < i2 - i1 <= MAX_BARS_BETWEEN:
            diff_pct = abs(p1 - p2) / p1 * 100
            if diff_pct <= TOLERANCE_PCT:
                seg = [candles[j]["l"] for j in range(i1 + 1, i2)]
                if seg:
                    neckline = min(seg)
                    base = min(p1, p2)
                    depth_pct = (base - neckline) / base * 100
                    if depth_pct >= MIN_DEPTH_PCT:
                        buffer = neckline * NECKLINE_BUFFER_PCT / 100
                        start = max(i2 + 1, n - CONFIRM_LOOKBACK)
                        for j in range(start, n):
                            if candles[j]["c"] < neckline - buffer:
                                # Trend filtresi: kırılım mumu EMA üstünde
                                # olmalı (yükselişte dönüş = gerçek tepe).
                                if candles[j]["c"] <= ema_vals[j]:
                                    break
                                results.append({
                                    "symbol": symbol, "pattern": "İKİLİ TEPE",
                                    "dir": "down", "p1": p1, "p2": p2,
                                    "neckline": neckline, "price": candles[-1]["c"],
                                    "break_bar": j, "break_t": candles[j]["t"],
                                })
                                break

    # ---- İkili dip (düşüş trendindeki dönüş) ----
    lows = pivot_lows(candles, PIVOT_LENGTH)
    if len(lows) >= 2:
        (i1, p1), (i2, p2) = lows[-2], lows[-1]
        if 0 < i2 - i1 <= MAX_BARS_BETWEEN:
            diff_pct = abs(p1 - p2) / p1 * 100
            if diff_pct <= TOLERANCE_PCT:
                seg = [candles[j]["h"] for j in range(i1 + 1, i2)]
                if seg:
                    neckline = max(seg)
                    base = max(p1, p2)
                    depth_pct = (neckline - base) / base * 100
                    if depth_pct >= MIN_DEPTH_PCT:
                        buffer = neckline * NECKLINE_BUFFER_PCT / 100
                        start = max(i2 + 1, n - CONFIRM_LOOKBACK)
                        for j in range(start, n):
                            if candles[j]["c"] > neckline + buffer:
                                # Trend filtresi: kırılım mumu EMA altında
                                # olmalı (düşüşte dönüş = gerçek dip).
                                if candles[j]["c"] >= ema_vals[j]:
                                    break
                                results.append({
                                    "symbol": symbol, "pattern": "İKİLİ DİP",
                                    "dir": "up", "p1": p1, "p2": p2,
                                    "neckline": neckline, "price": candles[-1]["c"],
                                    "break_bar": j, "break_t": candles[j]["t"],
                                })
                                break

    return results


def evaluate_symbol(symbol):
    candles = fetch_klines(symbol)
    min_bars = max(EMA_LENGTH, PIVOT_LENGTH * 2) + 5
    if not candles or len(candles) < min_bars:
        return []
    hits = detect_patterns(symbol, candles)
    if len(hits) > 1:
        # Aynı coinde iki formasyon çıktıysa sadece en taze kırılımı gönder.
        hits = [max(hits, key=lambda h: h["break_bar"])]
    return hits


# ---------------- Telegram ----------------
def send_telegram(text):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        _warn_once("tg_cfg", "TELEGRAM_TOKEN / TELEGRAM_CHAT_ID ayarlanmamış, bildirim atlandı.")
        return
    try:
        r = HTTP.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML",
                  "disable_web_page_preview": True},
            timeout=10,
        )
        if not r.json().get("ok"):
            log.error("Telegram hata: %s", r.text)
    except Exception as e:
        log.error("Telegram gönderim hatası: %s", e)


def notify(hit):
    key = (hit["symbol"], hit["pattern"])
    if key in already_alerted:
        return
    already_alerted.add(key)
    emoji = "🔻" if hit["dir"] == "down" else "🚀"
    break_time = time.strftime("%H:%M", time.gmtime(hit["break_t"] / 1000))
    log.info(
        "SİNYAL: %s %s %s | seviye1=%.8g seviye2=%.8g neckline=%.8g fiyat=%.8g kırılım=%sUTC",
        hit["symbol"], emoji, hit["pattern"],
        hit["p1"], hit["p2"], hit["neckline"], hit["price"], break_time,
    )
    msg = (
        f"<b>{hit['symbol']}</b> — {emoji} {hit['pattern']} ({TIMEFRAME})\n"
        f"Seviye 1: {hit['p1']:.8g}\n"
        f"Seviye 2: {hit['p2']:.8g}\n"
        f"Neckline: {hit['neckline']:.8g}\n"
        f"Fiyat: {hit['price']:.8g}\n"
        f"Kırılım: {break_time} UTC"
    )
    send_telegram(msg)


# ---------------- Ana döngü ----------------
def run_scan():
    coins = fetch_usdt_symbols()
    if MAX_COINS > 0:
        coins = coins[:MAX_COINS]
    if not coins:
        log.warning("Taranacak coin bulunamadı.")
        return
    log.info(
        "Tarama başlıyor: %d coin, TF=%s, tol=%%%.2f, min derinlik=%%%.2f, EMA=%d",
        len(coins), TIMEFRAME, TOLERANCE_PCT, MIN_DEPTH_PCT, EMA_LENGTH,
    )

    found = 0
    got_data = 0
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futures = {ex.submit(evaluate_symbol, sym): sym for sym in coins}
        for fut in as_completed(futures):
            sym = futures[fut]
            try:
                hits = fut.result()
            except Exception as e:
                log.debug("Sembol tarama hatası (%s): %s", sym, e)
                continue
            if hits:
                got_data += 1
            for hit in hits:
                found += 1
                notify(hit)

    log.info("Tarama bitti: %d coin tarandı, %d sinyal üretildi.", len(coins), found)
    if got_data == 0 and found == 0:
        log.error("Hiç veri/sinyal gelmedi! Muhtemelen Binance bu sunucunun IP'sini engelliyor.")


def main():
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log.warning("Telegram ayarlı değil — bildirimler sadece konsola düşecek.")
    log.info(
        "double-top-bottom-bot başlatıldı. TF=%s, SCAN_INTERVAL=%ss, PIVOT_LENGTH=%d, EMA=%d",
        TIMEFRAME, SCAN_INTERVAL_SEC, PIVOT_LENGTH, EMA_LENGTH,
    )
    while True:
        try:
            run_scan()
        except Exception as e:
            log.exception("Tarama döngüsünde beklenmeyen hata: %s", e)
        time.sleep(SCAN_INTERVAL_SEC)


if __name__ == "__main__":
    main()
