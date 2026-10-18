import os
import time
import threading
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
import pandas as pd
import requests

# ─────────────── AYARLAR (ENV'DEN OKUNUR) ───────────────
BREAK_LEN         = int(os.environ.get("BREAK_LEN", 20))
BO_BUFFER_PCT     = float(os.environ.get("BO_BUFFER_PCT", 0.1))
BO_VOL_MULT       = float(os.environ.get("BO_VOL_MULT", 1.5))
RSI_LEN           = int(os.environ.get("RSI_LEN", 14))
RSI_BULL          = float(os.environ.get("RSI_BULL", 55))
RSI_BEAR          = float(os.environ.get("RSI_BEAR", 45))
MIN_BARS_BETWEEN  = int(os.environ.get("MIN_BARS_BETWEEN", 5))
BODY_PCT          = float(os.environ.get("BODY_PCT", 3.0))   # kırılım mumu min gövde % (was 0)

VOL_MULT          = float(os.environ.get("VOL_MULT", 1.2))

MIN_VOLUME_USDT   = float(os.environ.get("MIN_VOLUME_USDT", 3_000_000))
TIMEFRAME         = os.environ.get("TIMEFRAME", "5m")
SCAN_INTERVAL_SEC = int(os.environ.get("SCAN_INTERVAL_SEC", 180))
MAX_WORKERS       = int(os.environ.get("MAX_WORKERS", 8))
WEIGHT_SOFT_LIMIT = int(os.environ.get("WEIGHT_SOFT_LIMIT", 1800))

BINANCE_FAPI      = "https://fapi.binance.com"
TELEGRAM_TOKEN    = os.environ.get("TELEGRAM_TOKEN", "BURAYA_TOKEN")
TELEGRAM_CHAT_ID  = os.environ.get("TELEGRAM_CHAT_ID", "BURAYA_CHAT_ID")

KLINES_LIMIT      = 200

http_session = requests.Session()
son_sinyal = {}

block_lock = threading.Lock()
blocked_until = 0.0

stats_lock = threading.Lock()
stats = {"kirilim": 0}


def send_telegram(text):
    if TELEGRAM_TOKEN == "BURAYA_TOKEN":
        print(text, flush=True)
        return
    try:
        r = http_session.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            data={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"},
            timeout=10,
        )
        if r.status_code != 200:
            print(f"Telegram HATA {r.status_code}: {r.text}", flush=True)
    except Exception as e:
        print(f"Telegram hatasi: {e}", flush=True)


def set_block(seconds, neden):
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
        print(f"API {r.status_code}: {r.text[:200]}", flush=True)
        return None
    try:
        return r.json()
    except Exception:
        return None


def hesapla_kirilim(c):
    close, high, low, open_, vol = c["close"], c["high"], c["low"], c["open"], c["volume"]

    delta = close.diff()
    ort_kazanc = delta.clip(lower=0).ewm(alpha=1 / RSI_LEN, adjust=False).mean()
    ort_kayip = (-delta.clip(upper=0)).ewm(alpha=1 / RSI_LEN, adjust=False).mean()
    rsi = 100 - 100 / (1 + ort_kazanc / ort_kayip)

    macd = close.ewm(span=12, adjust=False).mean() - close.ewm(span=26, adjust=False).mean()
    macd_sig = macd.ewm(span=9, adjust=False).mean()
    hist = macd - macd_sig

    direnc = high.rolling(BREAK_LEN).max().shift(1)
    destek = low.rolling(BREAK_LEN).min().shift(1)
    vol_sma = vol.rolling(20).mean()

    vol_ok = vol > vol_sma * BO_VOL_MULT
    body_ok = (close - open_).abs() / open_ * 100 >= BODY_PCT

    yukari = (close > direnc * (1 + BO_BUFFER_PCT / 100)) & (close.shift(1) <= direnc)
    asagi = (close < destek * (1 - BO_BUFFER_PCT / 100)) & (close.shift(1) >= destek)

    ham_al = yukari & vol_ok & body_ok & (rsi > RSI_BULL) & (hist > 0) & (macd > macd_sig)
    ham_sat = asagi & vol_ok & body_ok & (rsi < RSI_BEAR) & (hist < 0) & (macd < macd_sig)

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
