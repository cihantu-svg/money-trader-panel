"""
Wedge Pump Bot - basit takoz (wedge) kırılım tarayıcısı
--------------------------------------------------------
Bybit marjine açık USDT paritelerini periyodik olarak tarar, düşen takoz
(yukarı kırılım) ve yükselen takoz (aşağı kırılım) formasyonlarını arar.
Formasyon en az MIN_TOUCHES kez test edilmiş ve kırılım mumunun gövdesi
(open-close farkı) en az MIN_BODY_PCT ise Telegram'a bildirim gönderir.

Ortam değişkenleri (Render > Environment sekmesinde ayarlanır):
    TELEGRAM_TOKEN     - BotFather'dan alınan bot token (zorunlu)
    TELEGRAM_CHAT_ID   - Mesajın gideceği chat id (zorunlu)
    TIMEFRAME          - "1m" | "5m" | "15m" (varsayılan "15m")
    MIN_TOUCHES        - minimum temas sayısı (varsayılan 3)
    MIN_BODY_PCT       - kırılım mumunun min gövde yüzdesi (varsayılan 3.0)
    MAX_COINS          - 0 = tümü, aksi halde ilk N coin (varsayılan 0)
    SCAN_INTERVAL_SEC  - iki tarama arası bekleme (varsayılan 180)
    MAX_WORKERS        - eşzamanlı istek sayısı (varsayılan 8, rate-limit için düşük tutulur)
"""

import os
import time
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("wedge-pump-bot")

# ---------------- Ayarlar ----------------
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
TIMEFRAME = os.environ.get("TIMEFRAME", "15m")
MIN_TOUCHES = int(os.environ.get("MIN_TOUCHES", "3"))
MIN_BODY_PCT = float(os.environ.get("MIN_BODY_PCT", "3.0"))
MAX_COINS = int(os.environ.get("MAX_COINS", "0"))
SCAN_INTERVAL_SEC = int(os.environ.get("SCAN_INTERVAL_SEC", "180"))
MAX_WORKERS = int(os.environ.get("MAX_WORKERS", "8"))
BREAK_LOOKBACK = int(os.environ.get("BREAK_LOOKBACK", "2"))
KLINE_LIMIT = 300

BYB_INTERVAL = {"1m": "1", "5m": "5", "15m": "15", "30m": "30", "1h": "60", "4h": "240", "1d": "D"}
BIN_INTERVAL = {"1m": "1m", "5m": "5m", "15m": "15m", "30m": "30m", "1h": "1h", "4h": "4h", "1d": "1d"}

HTTP = requests.Session()
HTTP.headers.update({"User-Agent": "wedge-pump-bot/1.0"})

# aynı (sembol, yön) için tekrar tekrar bildirim atmamak için
already_alerted = set()
# her kaynağın ilk hatasını bir kere loglamak için (log spam'i önlemek için)
_warned_sources = set()


def _warn_once(key, msg):
    if key not in _warned_sources:
        _warned_sources.add(key)
        log.warning(msg)


# ---------------- Veri katmanı ----------------
def fetch_margin_coins():
    """Bybit'te marjin ile işlem görebilen USDT paritelerini döndürür."""
    try:
        r = HTTP.get(
            "https://api.bybit.com/v5/market/instruments-info",
            params={"category": "spot"}, timeout=15,
        )
        r.raise_for_status()
        data = r.json().get("result", {}).get("list", [])
        coins = [
            d["symbol"] for d in data
            if d.get("quoteCoin") == "USDT" and d.get("marginTrading", "none") != "none"
        ]
        if coins:
            return sorted(set(coins))
    except Exception as e:
        log.warning("Bybit marjin coin listesi alınamadı: %s", e)

    # yedek: Binance'te USDT paritelerinin tamamı (marjin filtresi olmadan)
    try:
        r = HTTP.get("https://data-api.binance.vision/api/v3/exchangeInfo", timeout=15)
        r.raise_for_status()
        syms = r.json().get("symbols", [])
        return sorted({s["symbol"] for s in syms if s.get("quoteAsset") == "USDT" and s.get("status") == "TRADING"})
    except Exception as e:
        log.error("Yedek coin listesi de alınamadı: %s", e)
        return []


def fetch_klines(symbol, tf):
    """[{o,h,l,c}] formatında kapanmış mumları döndürür (eskiden yeniye sıralı)."""
    # 1) Bybit spot
    try:
        r = HTTP.get(
            "https://api.bybit.com/v5/market/kline",
            params={"category": "spot", "symbol": symbol, "interval": BYB_INTERVAL[tf], "limit": KLINE_LIMIT},
            timeout=10,
        )
        if r.status_code != 200:
            _warn_once("byb_spot_kline", f"Bybit spot kline istekleri başarısız (örnek HTTP {r.status_code} - {symbol}). IP engeli/rate-limit olabilir.")
        else:
            rows = r.json().get("result", {}).get("list", [])
            if rows:
                rows = list(reversed(rows))  # bybit yeniden eskiye döner
                return [{"o": float(x[1]), "h": float(x[2]), "l": float(x[3]), "c": float(x[4])} for x in rows]
    except Exception as e:
        _warn_once("byb_spot_kline_exc", f"Bybit spot kline isteğinde istisna (örnek: {symbol}): {e}")
    # 2) Bybit linear (futures) - spotta yoksa
    try:
        r = HTTP.get(
            "https://api.bybit.com/v5/market/kline",
            params={"category": "linear", "symbol": symbol, "interval": BYB_INTERVAL[tf], "limit": KLINE_LIMIT},
            timeout=10,
        )
        if r.status_code != 200:
            _warn_once("byb_lin_kline", f"Bybit linear kline istekleri başarısız (örnek HTTP {r.status_code} - {symbol}).")
        else:
            rows = r.json().get("result", {}).get("list", [])
            if rows:
                rows = list(reversed(rows))
                return [{"o": float(x[1]), "h": float(x[2]), "l": float(x[3]), "c": float(x[4])} for x in rows]
    except Exception as e:
        _warn_once("byb_lin_kline_exc", f"Bybit linear kline isteğinde istisna (örnek: {symbol}): {e}")
    # 3) Binance (coğrafi blok bypass'lı public veri ucu)
    try:
        r = HTTP.get(
            "https://data-api.binance.vision/api/v3/klines",
            params={"symbol": symbol, "interval": BIN_INTERVAL[tf], "limit": KLINE_LIMIT},
            timeout=10,
        )
        if r.status_code != 200:
            _warn_once("bin_kline", f"Binance kline istekleri başarısız (örnek HTTP {r.status_code} - {symbol}).")
        else:
            rows = r.json()
            if isinstance(rows, list) and rows:
                return [{"o": float(x[1]), "h": float(x[2]), "l": float(x[3]), "c": float(x[4])} for x in rows]
    except Exception as e:
        _warn_once("bin_kline_exc", f"Binance kline isteğinde istisna (örnek: {symbol}): {e}")
    return None


# ---------------- Takoz (wedge) mantığı ----------------
def pivots(candles, window=5):
    """Fraktal pivot high/low listesi döndürür: [(bar_index, price), ...]"""
    highs, lows = [], []
    n = len(candles)
    for i in range(window, n - window):
        h = candles[i]["h"]
        l = candles[i]["l"]
        if all(h >= candles[j]["h"] for j in range(i - window, i + window + 1)):
            highs.append((i, h))
        if all(l <= candles[j]["l"] for j in range(i - window, i + window + 1)):
            lows.append((i, l))
    return highs, lows


def find_wedge(candles, direction):
    """direction: 'up' (düşen takoz, yukarı kırılım) | 'dn' (yükselen takoz, aşağı kırılım)"""
    highs, lows = pivots(candles, 5)
    n = len(candles)
    piv = [p for p in (highs if direction == "up" else lows) if p[0] >= n - 401][-10:]

    best = None
    for i in range(len(piv) - 1):
        for j in range(i + 1, len(piv)):
            b1, p1 = piv[i]
            b2, p2 = piv[j]
            if direction == "up" and not (p1 > p2):
                continue
            if direction == "dn" and not (p1 < p2):
                continue
            slope = (p2 - p1) / (b2 - b1)
            touches = 0
            prev_touch = False
            break_bar = None
            end = min(n - 1, b1 + 400)
            for b in range(b1, end + 1):
                yv = p1 + slope * (b - b1)
                c = candles[b]
                if direction == "up":
                    w, bd = c["h"], max(c["o"], c["c"])
                    if w > yv * 1.004 or bd > yv * 1.001:
                        break_bar = b
                        break
                    tz = w >= yv * 0.996
                else:
                    w, bd = c["l"], min(c["o"], c["c"])
                    if w < yv * 0.996 or bd < yv * 0.999:
                        break_bar = b
                        break
                    tz = w <= yv * 1.004
                if tz and not prev_touch:
                    touches += 1
                prev_touch = tz
            if best is None or touches > best["touches"]:
                best = {"p1": p1, "b1": b1, "slope": slope, "touches": touches, "break_bar": break_bar}
    return best


def evaluate_symbol(symbol, tf):
    """Döner: (hits, stats) — stats teşhis amaçlı (veri geldi mi, takoz bulundu mu, en iyi temas/gövde%)"""
    candles = fetch_klines(symbol, tf)
    stats = {"got_data": False, "any_wedge": False, "max_touches": 0, "body_pct": 0.0}
    if not candles or len(candles) < 60:
        return [], stats
    stats["got_data"] = True
    n = len(candles)
    results = []
    for direction in ("up", "dn"):
        w = find_wedge(candles, direction)
        if not w or w["break_bar"] is None or w["break_bar"] < n - BREAK_LOOKBACK:
            continue
        stats["any_wedge"] = True
        stats["max_touches"] = max(stats["max_touches"], w["touches"])
        brk = candles[w["break_bar"]]
        body_pct = abs(brk["c"] - brk["o"]) / brk["o"] * 100 if brk["o"] else 0
        stats["body_pct"] = max(stats["body_pct"], body_pct)
        if w["touches"] < MIN_TOUCHES:
            continue
        if body_pct < MIN_BODY_PCT:
            continue
        results.append({
            "symbol": symbol, "dir": direction, "tf": tf,
            "touches": w["touches"], "body_pct": body_pct, "price": candles[-1]["c"],
        })
    return results, stats


# ---------------- Telegram ----------------
def send_telegram(text):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log.warning("TELEGRAM_TOKEN / TELEGRAM_CHAT_ID ayarlanmamış, mesaj gönderilemedi.")
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
    key = (hit["symbol"], hit["dir"], hit["tf"])
    if key in already_alerted:
        return
    already_alerted.add(key)
    dir_txt = "🚀 TAKOZ YUKARI KIRILDI" if hit["dir"] == "up" else "🔻 TAKOZ AŞAĞI KIRILDI"
    tf_txt = {"1m": "1dk", "5m": "5dk", "15m": "15dk"}.get(hit["tf"], hit["tf"])
    msg = (
        f"<b>{hit['symbol']}</b> — {dir_txt} ({tf_txt})\n"
        f"Temas: {hit['touches']}\n"
        f"Mum Gövdesi: %{hit['body_pct']:.1f}\n"
        f"Fiyat: {hit['price']}"
    )
    log.info("SİNYAL: %s", msg.replace("\n", " | "))
    send_telegram(msg)


# ---------------- Ana döngü ----------------
def run_scan():
    coins = fetch_margin_coins()
    if MAX_COINS > 0:
        coins = coins[:MAX_COINS]
    if not coins:
        log.warning("Taranacak coin bulunamadı.")
        return
    log.info("Tarama başlıyor: %d coin, TF=%s, min temas=%d, min gövde=%%%.1f",
              len(coins), TIMEFRAME, MIN_TOUCHES, MIN_BODY_PCT)

    found = 0
    got_data = 0
    any_wedge = 0
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futures = {ex.submit(evaluate_symbol, sym, TIMEFRAME): sym for sym in coins}
        for fut in as_completed(futures):
            try:
                hits, stats = fut.result()
            except Exception as e:
                log.debug("Sembol tarama hatası (%s): %s", futures[fut], e)
                continue
            if stats["got_data"]:
                got_data += 1
            if stats["any_wedge"]:
                any_wedge += 1
            for hit in hits:
                found += 1
                notify(hit)
    log.info(
        "Tarama bitti: %d/%d coin'e veri geldi, %d coin'de takoz yapısı görüldü (eşik geçmemiş olabilir), %d sinyal filtreyi geçti.",
        got_data, len(coins), any_wedge, found,
    )
    if got_data == 0:
        log.error("HİÇBİR coin'e veri gelmedi! Muhtemelen Bybit/Binance bu sunucunun IP'sini engelliyor (403/451). Yukarıdaki uyarı satırlarına bak.")
    elif any_wedge == 0:
        log.info("Veri geldi ama hiç takoz yapısı (herhangi bir eşikte) bulunamadı — bu zaman diliminde şu an gerçekten sakin olabilir, normal bir durum.")


def main():
    log.info("wedge-pump-bot başlatıldı. TF=%s SCAN_INTERVAL=%ss", TIMEFRAME, SCAN_INTERVAL_SEC)
    while True:
        try:
            run_scan()
        except Exception as e:
            log.exception("Tarama döngüsünde beklenmeyen hata: %s", e)
        time.sleep(SCAN_INTERVAL_SEC)


if __name__ == "__main__":
    main()
