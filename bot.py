import os
import time
import threading
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
import pandas as pd
import requests

# ─────────────── AYARLAR ───────────────
TIMEFRAME         = os.environ.get("TIMEFRAME", "5m")
LOOK              = int(os.environ.get("LOOK", 30))                 # divergence penceresi (mum)
DIV_MIN_PCT       = float(os.environ.get("DIV_MIN_PCT", 5.0))       # CVD açığı / pencere hacmi, %
MIN_DELTA_USDT    = float(os.environ.get("MIN_DELTA_USDT", 150_000))  # tek mum net delta (USDT)
DELTA_RATIO       = float(os.environ.get("DELTA_RATIO", 0.35))      # |delta| / hacim
DELTA_X           = float(os.environ.get("DELTA_X", 3.0))           # normal |delta| ortalamasının kaç katı

MIN_VOLUME_USDT   = float(os.environ.get("MIN_VOLUME_USDT", 3_000_000))
SCAN_INTERVAL_SEC = int(os.environ.get("SCAN_INTERVAL_SEC", 180))
MAX_WORKERS       = int(os.environ.get("MAX_WORKERS", 8))
WEIGHT_SOFT_LIMIT = int(os.environ.get("WEIGHT_SOFT_LIMIT", 1800))

BINANCE_FAPI      = "https://fapi.binance.com"
TELEGRAM_TOKEN    = os.environ.get("TELEGRAM_TOKEN", "BURAYA_TOKEN")
TELEGRAM_CHAT_ID  = os.environ.get("TELEGRAM_CHAT_ID", "BURAYA_CHAT_ID")

http_session = requests.Session()
son_sinyal = {}
block_lock = threading.Lock()
blocked_until = 0.0


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
        try:
            bekleme = float(r.headers.get("Retry-After"))
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


def para(x):
    """USDT'yi okunur yaz: 1.2M, 350K"""
    a = abs(x)
    s = f"{a/1e6:.2f}M" if a >= 1e6 else f"{a/1e3:.0f}K"
    return ("-" if x < 0 else "+") + s


def check_symbol(symbol):
    data = api_get("/fapi/v1/klines", {"symbol": symbol, "interval": TIMEFRAME, "limit": 200})
    if not isinstance(data, list) or len(data) < LOOK + 30:
        return []

    df = pd.DataFrame(data, columns=[
        "open_time", "open", "high", "low", "close", "volume",
        "close_time", "quote_volume", "trades",
        "taker_buy_base", "taker_buy_quote", "ignore"
    ])
    for col in ["open", "high", "low", "close", "quote_volume", "taker_buy_quote"]:
        df[col] = df[col].astype(float)

    c = df.iloc[:-1].reset_index(drop=True)          # sadece kapanmış mumlar
    delta = 2 * c["taker_buy_quote"] - c["quote_volume"]   # USDT cinsinden net delta
    cvd = delta.cumsum()
    qv = c["quote_volume"]
    close = c["close"]
    ot = int(c["open_time"].iloc[-1])
    sonuc = []

    # 1) CVD DIVERGENCE
    pencere_hacim = qv.iloc[-LOOK:].sum()
    onceki_close = close.iloc[-LOOK - 1:-1]
    onceki_cvd = cvd.iloc[-LOOK - 1:-1]

    if close.iloc[-1] >= onceki_close.max():
        acik = onceki_cvd.max() - cvd.iloc[-1]
        guc = acik / pencere_hacim * 100
        if guc >= DIV_MIN_PCT:
            sonuc.append(("ZAYIF YÜKSELİŞ", "🟠", ot, guc, acik))
    elif close.iloc[-1] <= onceki_close.min():
        acik = cvd.iloc[-1] - onceki_cvd.min()
        guc = acik / pencere_hacim * 100
        if guc >= DIV_MIN_PCT:
            sonuc.append(("ZAYIF DÜŞÜŞ", "🔵", ot, guc, -acik))

    # 2) DELTA PATLAMASI
    d = delta.iloc[-1]
    oran = d / qv.iloc[-1] if qv.iloc[-1] > 0 else 0
    normal = delta.iloc[-21:-1].abs().mean()
    if abs(d) >= MIN_DELTA_USDT and abs(oran) >= DELTA_RATIO and normal > 0 and abs(d) >= normal * DELTA_X:
        yon = "ALIM PATLAMASI" if d > 0 else "SATIŞ PATLAMASI"
        ikon = "🟢" if d > 0 else "🔴"
        sonuc.append((yon, ikon, ot, abs(d) / normal, d))

    out = []
    for tip, ikon, ot_, guc, tutar in sonuc:
        out.append({
            "symbol": symbol, "tip": tip, "ikon": ikon, "open_time": ot_,
            "guc": guc, "tutar": tutar, "fiyat": close.iloc[-1],
            "delta": d, "oran": oran, "cvd_son": cvd.iloc[-1] - cvd.iloc[-LOOK - 1],
        })
    return out


def scan_once():
    print(f"\n[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] Tarama başladı...", flush=True)
    tickers = api_get("/fapi/v1/ticker/24hr")
    if not isinstance(tickers, list):
        print("Ticker alınamadı, tarama atlandı.", flush=True)
        return

    semboller = [d["symbol"] for d in tickers
                 if d["symbol"].endswith("USDT") and float(d.get("quoteVolume", 0)) >= MIN_VOLUME_USDT]

    bulunan = 0
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futures = {ex.submit(check_symbol, s): s for s in semboller}
        for f in as_completed(futures):
            symbol = futures[f]
            try:
                liste = f.result()
            except Exception as e:
                print(f"{symbol} hata: {e}", flush=True)
                continue
            for s in liste:
                key = (symbol, s["tip"])
                if son_sinyal.get(key) == s["open_time"]:
                    continue
                son_sinyal[key] = s["open_time"]
                bulunan += 1

                if "PATLAMASI" in s["tip"]:
                    ozel = f"• <b>Net delta</b>: {para(s['tutar'])} USDT (normalin {s['guc']:.1f}x)\n"
                else:
                    ozel = (f"• <b>CVD açığı</b>: {para(s['tutar'])} USDT (pencere hacminin %{s['guc']:.1f}'i)\n")

                send_telegram(
                    f"{s['ikon']} <b>{s['tip']}</b>\n\n"
                    f"• <b>Sembol</b>: {symbol}\n"
                    f"{ozel}"
                    f"• <b>Son mum delta</b>: {para(s['delta'])} USDT (%{s['oran']*100:+.0f})\n"
                    f"• <b>CVD ({LOOK} mum)</b>: {para(s['cvd_son'])} USDT\n"
                    f"• <b>Fiyat</b>: {s['fiyat']}\n"
                    f"• <b>Zaman Dilimi</b>: {TIMEFRAME}"
                )
                print(f"[SİNYAL] {symbol} {s['tip']}", flush=True)

    print(f"Tarama bitti. {len(semboller)} sembol | {bulunan} yeni sinyal.", flush=True)


def main():
    print(f"Delta/CVD botu başladı | TF={TIMEFRAME} | LOOK={LOOK}", flush=True)
    send_telegram("✅ Delta/CVD botu başlatıldı.")
    while True:
        t0 = time.time()
        try:
            scan_once()
        except Exception as e:
            print(f"Genel hata: {e}", flush=True)
        time.sleep(max(SCAN_INTERVAL_SEC - (time.time() - t0), block_remaining(), 1))


if __name__ == "__main__":
    main()
