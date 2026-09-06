"""
4H Trendline Break Bot
----------------------
Bybit marjine açık USDT paritelerini 4 saatlik grafikte periyodik olarak
tarar. LuxAlgo "Trendlines with Breaks" göstergesinin ATR eğim mantığını
kullanır: son pivot noktasından ATR bazlı bir eğimle projekte edilen
trendline'ı fiyat kapanışla kırdığında ve kırılım mumunun gövdesi en az
MIN_BODY_PCT ise Telegram'a bildirim gönderir.

Sadece KAPANMIŞ mumlarla çalışır (henüz oluşmakta olan son mum otomatik
atılır) — bu yüzden orijinal Pine göstergesindeki repaint sorunu burada
yoktur.

Ortam değişkenleri:
    TELEGRAM_TOKEN     - BotFather'dan alınan bot token (zorunlu)
    TELEGRAM_CHAT_ID   - Mesajın gideceği chat id (zorunlu)
    TL_LENGTH          - pivot lookback (varsayılan 14)
    TL_MULT            - eğim çarpanı (varsayılan 1.0)
    MIN_BODY_PCT       - kırılım mumunun min gövde yüzdesi (varsayılan 4.0)
    BREAK_LOOKBACK     - kırılımın son kaç mum içinde sayılacağı (varsayılan 2)
    MAX_COINS          - 0 = tümü, aksi halde ilk N coin (varsayılan 0)
    SCAN_INTERVAL_SEC  - iki tarama arası bekleme, saniye (varsayılan 180)
    MAX_WORKERS        - eşzamanlı istek sayısı (varsayılan 8)
"""

import os
import time
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("4h-trendline-bot")

# ---------------- Ayarlar ----------------
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
TIMEFRAME = "4h"
TL_LENGTH = int(os.environ.get("TL_LENGTH", "14"))
TL_MULT = float(os.environ.get("TL_MULT", "1.0"))
MIN_BODY_PCT = float(os.environ.get("MIN_BODY_PCT", "4.0"))
BREAK_LOOKBACK = int(os.environ.get("BREAK_LOOKBACK", "2"))
MAX_COINS = int(os.environ.get("MAX_COINS", "0"))
SCAN_INTERVAL_SEC = int(os.environ.get("SCAN_INTERVAL_SEC", "180"))
MAX_WORKERS = int(os.environ.get("MAX_WORKERS", "8"))
KLINE_LIMIT = 300
INTERVAL_MS = 14_400_000  # 4 saat

HTTP = requests.Session()
HTTP.headers.update({"User-Agent": "4h-trendline-bot/1.0"})

already_alerted = set()
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

    try:
        r = HTTP.get("https://data-api.binance.vision/api/v3/exchangeInfo", timeout=15)
        r.raise_for_status()
        syms = r.json().get("symbols", [])
        return sorted({s["symbol"] for s in syms if s.get("quoteAsset") == "USDT" and s.get("status") == "TRADING"})
    except Exception as e:
        log.error("Yedek coin listesi de alınamadı: %s", e)
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
    """[{t,o,h,l,c}] formatında SADECE KAPANMIŞ 4H mumları döndürür (eskiden yeniye)."""
    try:
        r = HTTP.get(
            "https://api.bybit.com/v5/market/kline",
            params={"category": "spot", "symbol": symbol, "interval": "240", "limit": KLINE_LIMIT},
            timeout=10,
        )
        if r.status_code != 200:
            _warn_once("byb_spot", f"Bybit spot kline başarısız (HTTP {r.status_code} - {symbol}).")
        else:
            rows = r.json().get("result", {}).get("list", [])
            if rows:
                rows = list(reversed(rows))
                candles = [{"t": int(x[0]), "o": float(x[1]), "h": float(x[2]), "l": float(x[3]), "c": float(x[4])} for x in rows]
                return _drop_unclosed(candles)
    except Exception as e:
        _warn_once("byb_spot_exc", f"Bybit spot kline isteğinde istisna (örnek: {symbol}): {e}")

    try:
        r = HTTP.get(
            "https://api.bybit.com/v5/market/kline",
            params={"category": "linear", "symbol": symbol, "interval": "240", "limit": KLINE_LIMIT},
            timeout=10,
        )
        if r.status_code != 200:
            _warn_once("byb_lin", f"Bybit linear kline başarısız (HTTP {r.status_code} - {symbol}).")
        else:
            rows = r.json().get("result", {}).get("list", [])
            if rows:
                rows = list(reversed(rows))
                candles = [{"t": int(x[0]), "o": float(x[1]), "h": float(x[2]), "l": float(x[3]), "c": float(x[4])} for x in rows]
                return _drop_unclosed(candles)
    except Exception as e:
        _warn_once("byb_lin_exc", f"Bybit linear kline isteğinde istisna (örnek: {symbol}): {e}")

    try:
        r = HTTP.get(
            "https://data-api.binance.vision/api/v3/klines",
            params={"symbol": symbol, "interval": "4h", "limit": KLINE_LIMIT},
            timeout=10,
        )
        if r.status_code != 200:
            _warn_once("bin", f"Binance kline başarısız (HTTP {r.status_code} - {symbol}).")
        else:
            rows = r.json()
            if isinstance(rows, list) and rows:
                candles = [{"t": int(x[0]), "o": float(x[1]), "h": float(x[2]), "l": float(x[3]), "c": float(x[4])} for x in rows]
                return _drop_unclosed(candles)
    except Exception as e:
        _warn_once("bin_exc", f"Binance kline isteğinde istisna (örnek: {symbol}): {e}")
    return None


# ---------------- Trendline (ATR eğimli) mantığı ----------------
def wilder_atr(candles, length):
    n = len(candles)
    tr = [0.0] * n
    for i in range(n):
        h, l = candles[i]["h"], candles[i]["l"]
        if i == 0:
            tr[i] = h - l
        else:
            pc = candles[i - 1]["c"]
            tr[i] = max(h - l, abs(h - pc), abs(l - pc))
    atr = [None] * n
    if n < length:
        return atr
    atr[length - 1] = sum(tr[0:length]) / length
    for i in range(length, n):
        atr[i] = (atr[i - 1] * (length - 1) + tr[i]) / length
    return atr


def _pivot_high_at(candles, i, length):
    n = len(candles)
    if i - length < 0 or i + length >= n:
        return None
    h = candles[i]["h"]
    if all(h >= candles[j]["h"] for j in range(i - length, i + length + 1)):
        return h
    return None


def _pivot_low_at(candles, i, length):
    n = len(candles)
    if i - length < 0 or i + length >= n:
        return None
    l = candles[i]["l"]
    if all(l <= candles[j]["l"] for j in range(i - length, i + length + 1)):
        return l
    return None


def trendline_breaks(candles, length, mult):
    """Döner: [{'bar','dir','body_pct'}, ...] — o barda gerçekleşen kırılımlar."""
    n = len(candles)
    atr = wilder_atr(candles, length)
    upper = lower = slope_ph = slope_pl = 0.0
    upos = dnos = 0
    breaks = []
    for i in range(n):
        a = atr[i] if atr[i] is not None else 0.0
        slope = a / length * mult if length else 0.0
        ph = _pivot_high_at(candles, i, length)
        pl = _pivot_low_at(candles, i, length)
        if ph is not None:
            slope_ph = slope
        if pl is not None:
            slope_pl = slope
        upper = ph if ph is not None else upper - slope_ph
        lower = pl if pl is not None else lower + slope_pl
        c = candles[i]["c"]
        new_upos = 0 if ph is not None else (1 if c > upper - slope_ph * length else upos)
        new_dnos = 0 if pl is not None else (1 if c < lower + slope_pl * length else dnos)
        o = candles[i]["o"]
        body_pct = abs(c - o) / o * 100 if o else 0.0
        if new_upos > upos:
            breaks.append({"bar": i, "dir": "up", "body_pct": body_pct})
        if new_dnos > dnos:
            breaks.append({"bar": i, "dir": "dn", "body_pct": body_pct})
        upos, dnos = new_upos, new_dnos
    return breaks


def evaluate_symbol(symbol):
    """Döner: (hits, stats)."""
    candles = fetch_klines(symbol)
    stats = {"got_data": False, "any_break": False, "body_pct": 0.0}
    if not candles or len(candles) < TL_LENGTH * 2 + 5:
        return [], stats
    stats["got_data"] = True
    n = len(candles)
    breaks = trendline_breaks(candles, TL_LENGTH, TL_MULT)
    recent = [b for b in breaks if b["bar"] >= n - BREAK_LOOKBACK]
    if recent:
        stats["any_break"] = True
        stats["body_pct"] = max(b["body_pct"] for b in recent)
    results = []
    for b in recent:
        if b["body_pct"] < MIN_BODY_PCT:
            continue
        results.append({
            "symbol": symbol, "dir": b["dir"],
            "body_pct": b["body_pct"], "price": candles[-1]["c"],
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
    key = (hit["symbol"], hit["dir"])
    if key in already_alerted:
        return
    already_alerted.add(key)
    dir_txt = "🚀 YUKARI KIRILDI" if hit["dir"] == "up" else "🔻 AŞAĞI KIRILDI"
    msg = (
        f"<b>{hit['symbol']}</b> — {dir_txt} (4S)\n"
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
    log.info("Tarama başlıyor: %d coin, TF=4H, min gövde=%%%.1f", len(coins), MIN_BODY_PCT)

    found = 0
    got_data = 0
    any_break = 0
    break_details = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futures = {ex.submit(evaluate_symbol, sym): sym for sym in coins}
        for fut in as_completed(futures):
            sym = futures[fut]
            try:
                hits, stats = fut.result()
            except Exception as e:
                log.debug("Sembol tarama hatası (%s): %s", sym, e)
                continue
            if stats["got_data"]:
                got_data += 1
            if stats["any_break"]:
                any_break += 1
                break_details.append(f"{sym} (gövde=%{stats['body_pct']:.1f})")
            for hit in hits:
                found += 1
                notify(hit)

    log.info(
        "Tarama bitti: %d/%d coin'e veri geldi, %d coin'de kırılım görüldü, %d sinyal filtreyi geçti.",
        got_data, len(coins), any_break, found,
    )
    if break_details:
        log.info("Kırılım görülen coinler: %s", ", ".join(break_details))
    if got_data == 0:
        log.error("HİÇBİR coin'e veri gelmedi! Muhtemelen Bybit/Binance bu sunucunun IP'sini engelliyor.")


def main():
    log.info("4h-trendline-bot başlatıldı. SCAN_INTERVAL=%ss, MIN_BODY_PCT=%%%.1f", SCAN_INTERVAL_SEC, MIN_BODY_PCT)
    while True:
        try:
            run_scan()
        except Exception as e:
            log.exception("Tarama döngüsünde beklenmeyen hata: %s", e)
        time.sleep(SCAN_INTERVAL_SEC)


if __name__ == "__main__":
    main()
