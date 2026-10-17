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


def check_symbol(symbol):
    data = api_get("/fapi/v1/klines",
                   {"symbol": symbol, "interval": TIMEFRAME, "limit": KLINES_LIMIT})
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
    c = df.iloc[:-1].reset_index(drop=True)
    B = c.iloc[-1]   # teyit mumu (son kapanan)
    A = c.iloc[-2]   # kırılım mumu

    yon_listesi, direnc, destek = hesapla_kirilim(c)
    yon = yon_listesi[-2]
    if yon is None:
        return None
    seviye = direnc.iloc[-2] if yon == "AL" else destek.iloc[-2]

    # Teyit mumu: kırılan seviyenin doğru tarafında kapanış + hacim + taker oranı
    baz_hacim = c["volume"].iloc[-22:-2].mean()  # kırılım mumundan önceki 20 mum
    if baz_hacim <= 0 or B["volume"] <= 0:
        return None
    hacim_x = B["volume"] / baz_hacim
    taker_buy = B["taker_buy_base"] / B["volume"]

    if yon == "AL":
        ok = B["close"] > seviye and hacim_x >= VOL_MULT and taker_buy >= TAKER_BUY_MIN
    else:
        ok = B["close"] < seviye and hacim_x >= VOL_MULT and taker_buy <= (1 - TAKER_BUY_MIN)
    if not ok:
        return None

    return {
        "symbol": symbol,
        "yon": yon,
        "open_time": int(B["open_time"]),
        "body_pct": abs(A["close"] - A["open"]) / A["open"] * 100,
        "hacim_x": hacim_x,
        "taker_buy": taker_buy,
        "close": B["close"],
        "seviye": seviye,
        "uzaklik": (B["close"] - seviye) / seviye * 100,
    }


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

    print(f"Tarama bitti. {bulunan} yeni sinyal.", flush=True)


def main():
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
