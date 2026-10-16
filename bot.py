import os
import time
import threading
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
import pandas as pd
import requests

# ─────────────── AYARLAR (ENV'DEN OKUNUR) ───────────────
SMA_LEN           = int(os.environ.get("SMA_LEN", 100))
BODY_PCT          = float(os.environ.get("BODY_PCT", 5.0))          # sinyal mumu min gövde %
MIN_VOLUME_USDT   = float(os.environ.get("MIN_VOLUME_USDT", 3_000_000))
TIMEFRAME         = os.environ.get("TIMEFRAME", "5m")
SCAN_INTERVAL_SEC = int(os.environ.get("SCAN_INTERVAL_SEC", 180))   # 3 dk
MAX_WORKERS       = int(os.environ.get("MAX_WORKERS", 8))

VOL_MULT          = float(os.environ.get("VOL_MULT", 1.5))          # teyit mumu hacim çarpanı
TAKER_BUY_MIN     = float(os.environ.get("TAKER_BUY_MIN", 0.55))    # AL için alıcı oranı
NEAR_TOUCH_PCT    = float(os.environ.get("NEAR_TOUCH_PCT", 0.3))    # SMA'ya yakınlık %

WEIGHT_SOFT_LIMIT = int(os.environ.get("WEIGHT_SOFT_LIMIT", 1800))  # Binance limiti 2400/dk

BINANCE_FAPI      = "https://fapi.binance.com"
TELEGRAM_TOKEN    = os.environ.get("TELEGRAM_TOKEN", "BURAYA_TOKEN")
TELEGRAM_CHAT_ID  = os.environ.get("TELEGRAM_CHAT_ID", "BURAYA_CHAT_ID")

KLINES_LIMIT      = SMA_LEN + 12

http_session = requests.Session()
son_sinyal = {}  # {symbol: teyit mumu open_time} -> aynı mumda tekrar göndermemek için

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
    """Tüm Binance isteklerinin geçtiği tek nokta: 418/429 ve weight koruması burada."""
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

    # Limite yaklaşınca dakika sıfırlanana kadar dur (418'e düşmemek için)
    used = r.headers.get("X-MBX-USED-WEIGHT-1M")
    if used and used.isdigit() and int(used) >= WEIGHT_SOFT_LIMIT:
        set_block(61 - (time.time() % 60), "weight")

    if r.status_code != 200:
        return None
    try:
        return r.json()
    except Exception:
        return None


def touches_sma(row, prev_row, sma, prev_sma):
    direct = row["low"] <= sma <= row["high"]
    near = (abs(row["low"] - sma) / sma * 100 <= NEAR_TOUCH_PCT or
            abs(row["high"] - sma) / sma * 100 <= NEAR_TOUCH_PCT)
    bull_cross = prev_row["close"] < prev_sma and row["close"] > sma
    bear_cross = prev_row["close"] > prev_sma and row["close"] < sma
    return direct or near or bull_cross or bear_cross


def check_symbol(symbol):
    data = api_get("/fapi/v1/klines",
                   {"symbol": symbol, "interval": TIMEFRAME, "limit": KLINES_LIMIT})
    if not isinstance(data, list) or len(data) < SMA_LEN + 8:
        return None

    df = pd.DataFrame(data, columns=[
        "open_time", "open", "high", "low", "close", "volume",
        "close_time", "quote_volume", "trades",
        "taker_buy_base", "taker_buy_quote", "ignore"
    ])
    for col in ["open", "high", "low", "close", "volume", "taker_buy_base"]:
        df[col] = df[col].astype(float)
    df["sma"] = df["close"].rolling(SMA_LEN).mean()

    # Son mum canlı -> at. Sadece KAPANMIŞ mumlarla çalış.
    c = df.iloc[:-1].reset_index(drop=True)
    B = c.iloc[-1]       # teyit mumu (son kapanan)
    A = c.iloc[-2]       # sinyal mumu
    prevA = c.iloc[-3]

    if pd.isna(A["sma"]) or pd.isna(prevA["sma"]) or pd.isna(B["sma"]):
        return None

    # 1) Sinyal mumu: %5+ gövde + SMA100 teması
    body_pct = abs(A["close"] - A["open"]) / A["open"] * 100
    if body_pct < BODY_PCT:
        return None
    if not touches_sma(A, prevA, A["sma"], prevA["sma"]):
        return None

    yon = "AL" if A["close"] > A["open"] else "SAT"

    # 2) Teyit mumu: SMA'nın doğru tarafında kapanış + hacim + taker oranı
    baz_hacim = c["volume"].iloc[-22:-2].mean()  # sinyal mumundan önceki 20 mum
    if baz_hacim <= 0 or B["volume"] <= 0:
        return None
    hacim_x = B["volume"] / baz_hacim
    taker_buy = B["taker_buy_base"] / B["volume"]

    if yon == "AL":
        ok = (B["close"] > B["sma"] and hacim_x >= VOL_MULT and taker_buy >= TAKER_BUY_MIN)
    else:
        ok = (B["close"] < B["sma"] and hacim_x >= VOL_MULT and taker_buy <= (1 - TAKER_BUY_MIN))
    if not ok:
        return None

    return {
        "symbol": symbol,
        "yon": yon,
        "open_time": int(B["open_time"]),
        "body_pct": body_pct,
        "hacim_x": hacim_x,
        "taker_buy": taker_buy,
        "close": B["close"],
        "sma": B["sma"],
        "uzaklik": (B["close"] - B["sma"]) / B["sma"] * 100,
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
                f"{ikon} <b>SMA{SMA_LEN} TEYİTLİ {s['yon']}</b>\n\n"
                f"• <b>Sembol</b>: {s['symbol']}\n"
                f"• <b>Sinyal mumu gövde</b>: %{s['body_pct']:.2f}\n"
                f"• <b>Teyit hacmi</b>: {s['hacim_x']:.1f}x\n"
                f"• <b>Alıcı oranı</b>: %{s['taker_buy'] * 100:.0f}\n"
                f"• <b>Fiyat</b>: {s['close']}\n"
                f"• <b>SMA{SMA_LEN}</b>: {s['sma']:.6f} (%{s['uzaklik']:+.2f})\n"
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
        # Ban/limit varsa bitene kadar bekle, yoksa normal aralığı tamamla
        bekle = max(SCAN_INTERVAL_SEC - (time.time() - baslangic), block_remaining(), 1)
        time.sleep(bekle)


if __name__ == "__main__":
    main()
