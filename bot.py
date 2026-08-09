# -*- coding: utf-8 -*-
"""
15DK DIRENC KIRILIM SCANNER BOT - v2 (ONAY PENCERELI)
========================================================
Degisiklik ozeti (v1'e gore):
- Kirilim tespit edildiginde ARTIK HEMEN Telegram'a gitmiyor.
- WATCH_WINDOW_MIN (varsayilan 20dk) boyunca 1dk mumlarla izleniyor:
    * Bu sure icinde fiyat kirilan direncin altina inerse -> IPTAL (sessiz log, Telegram YOK)
    * Sure sonunda hala gecerliyse -> Telegram'a gonderilir ("ONAYLANMIS SINYAL")
- Bu, 30 gunluk backtest'te "erken cokme" gosteren sinyallerin ~%100 basari ile
  onceden elenebildigi bulgusuna dayanir (bkz. gecmis backtest sonuclari).
- 15dk grafikte son N mumun en yuksegini (direnc) hesaplar (SADECE KAPANMIS mumlar - repaint yok)
- Kirilim mumu direnci gecmis, YESIL olmali, govdesi en az MIN_CANDLE_BODY_PCT olmali
- Coin'in son 24s USDT hacmi MIN_QUOTE_VOLUME_24H altindaysa sinyal uretilmez
"""
import os
import time
import logging
from datetime import datetime, timedelta
from dataclasses import dataclass
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
import pandas as pd

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════════════
# AYARLAR
# ══════════════════════════════════════════════════════════════════
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

SCAN_INTERVAL = int(os.getenv("SCAN_INTERVAL", "300"))
MAX_COINS = int(os.getenv("MAX_COINS", "600"))
SIGNAL_COOLDOWN = int(os.getenv("SIGNAL_COOLDOWN", "3600"))
MAX_WORKERS = int(os.getenv("MAX_WORKERS", "20"))
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "8"))

TIMEFRAME = os.getenv("TIMEFRAME", "15m")
RES_LOOKBACK = int(os.getenv("RES_LOOKBACK", "50"))
RES_BREAK_PCT = float(os.getenv("RES_BREAK_PCT", "0.5"))
MIN_CANDLE_BODY_PCT = float(os.getenv("MIN_CANDLE_BODY_PCT", "10.0"))

USE_LIQUIDITY_FILTER = os.getenv("USE_LIQUIDITY_FILTER", "true").lower() == "true"
MIN_QUOTE_VOLUME_24H = float(os.getenv("MIN_QUOTE_VOLUME_24H", "5000000"))

# --- YENI: ONAY PENCERESI AYARLARI ---
WATCH_WINDOW_MIN = int(os.getenv("WATCH_WINDOW_MIN", "20"))   # backtest'te en iyi sonucu veren deger
PENDING_CHECK_LIMIT = int(os.getenv("PENDING_CHECK_LIMIT", "60"))  # 1dk mum cekme limiti (guvenlik payi)

BINANCE_BASE = "https://fapi.binance.com"
last_signal = {}          # cooldown takibi (onaylanip gonderilen sinyaller icin)
processed_bars = {}       # her sembol icin son islenen 15dk mumun bar_time'i (tekrar eklemeyi onler)
pending_signals = {}      # onay bekleyen sinyaller: {symbol: {...}}


# ══════════════════════════════════════════════════════════════════
# BINANCE VERI CEKME
# ══════════════════════════════════════════════════════════════════
def get_symbols():
    try:
        session = requests.Session()
        r = session.get(f"{BINANCE_BASE}/fapi/v1/exchangeInfo", timeout=10)
        data = r.json()
        syms = [s["symbol"] for s in data["symbols"]
                if s["symbol"].endswith("USDT") and s["status"] == "TRADING"]
        return syms[:MAX_COINS]
    except Exception as e:
        log.error(f"get_symbols hata: {e}")
        return []


def get_klines(symbol, interval, limit=200, start_ms=None, end_ms=None):
    try:
        session = requests.Session()
        params = {"symbol": symbol, "interval": interval, "limit": limit}
        if start_ms is not None:
            params["startTime"] = start_ms
        if end_ms is not None:
            params["endTime"] = end_ms
        r = session.get(f"{BINANCE_BASE}/fapi/v1/klines", params=params, timeout=REQUEST_TIMEOUT)
        raw = r.json()
        df = pd.DataFrame(raw, columns=[
            "open_time", "open", "high", "low", "close", "volume", "close_time",
            "qav", "trades", "tbv", "tqv", "ignore"])
        for c in ["open", "high", "low", "close", "volume"]:
            df[c] = df[c].astype(float)
        df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
        return df
    except Exception:
        return None


def get_klines_closed(symbol, interval, limit=200):
    """Sadece KAPANMIS mumlari doner - repaint/titresim onlenir."""
    df = get_klines(symbol, interval, limit=limit + 1)
    if df is None or len(df) < 2:
        return None
    return df.iloc[:-1].reset_index(drop=True)


def get_all_24h_quote_volumes():
    try:
        session = requests.Session()
        r = session.get(f"{BINANCE_BASE}/fapi/v1/ticker/24hr", timeout=15)
        data = r.json()
        return {d["symbol"]: float(d.get("quoteVolume", 0)) for d in data}
    except Exception as e:
        log.error(f"get_all_24h_quote_volumes hata: {e}")
        return {}


# ══════════════════════════════════════════════════════════════════
# SINYAL: DIRENC KIRILIMI (TESPIT - henuz bildirim yok)
# ══════════════════════════════════════════════════════════════════
@dataclass
class Signal:
    symbol: str
    price: float
    resistance: float
    break_pct: float
    body_pct: float
    bar_time: str
    breakout_close_time: object   # 15dk mumun KAPANIS zamani (pd.Timestamp, UTC) -> onay penceresi buradan baslar


def analyze_symbol(symbol, quote_volumes=None):
    if USE_LIQUIDITY_FILTER:
        qv = (quote_volumes or {}).get(symbol, 0.0)
        if qv < MIN_QUOTE_VOLUME_24H:
            return None

    df = get_klines_closed(symbol, TIMEFRAME, limit=RES_LOOKBACK + 5)
    if df is None or len(df) < RES_LOOKBACK + 1:
        return None

    breakout = df.iloc[-1]
    history = df.iloc[-(RES_LOOKBACK + 1):-1]

    resistance = float(history["high"].max())
    open_now = float(breakout["open"])
    close_now = float(breakout["close"])
    bar_time = str(breakout["open_time"])
    breakout_close_time = breakout["open_time"] + timedelta(minutes=15)  # 15dk mumun kapanis ani

    if close_now <= open_now:
        return None
    if close_now <= resistance:
        return None

    break_pct = (close_now - resistance) / resistance * 100
    body_pct = (close_now - open_now) / open_now * 100

    if break_pct < RES_BREAK_PCT:
        return None
    if body_pct < MIN_CANDLE_BODY_PCT:
        return None

    return Signal(
        symbol=symbol, price=close_now, resistance=resistance,
        break_pct=break_pct, body_pct=body_pct, bar_time=bar_time,
        breakout_close_time=breakout_close_time,
    )


# ══════════════════════════════════════════════════════════════════
# YENI: ONAY PENCERESI KONTROLU (1DK MUMLARLA ERKEN GECERSIZLIK TESPITI)
# ══════════════════════════════════════════════════════════════════
def check_pending_confirmations():
    """Bekleyen sinyalleri 1dk mumlarla kontrol eder:
    - Direncin altina inildiyse -> iptal (sessiz)
    - WATCH_WINDOW_MIN doldu ve hala gecerliyse -> onaylanmis sinyal olarak dondur
    Donen: onaylanan Signal nesnelerinin listesi (Telegram'a gonderilecekler)."""
    confirmed = []

    for symbol in list(pending_signals.keys()):
        info = pending_signals[symbol]
        breakout_close_time = info["breakout_close_time"]
        resistance = info["resistance"]

        elapsed_min = (pd.Timestamp.now(tz="UTC") - breakout_close_time).total_seconds() / 60

        # 1dk mumlarla, kirilim kapanisindan simdiye kadar olan araligi kontrol et
        df1m = get_klines(symbol, "1m", limit=PENDING_CHECK_LIMIT,
                           start_ms=int(breakout_close_time.timestamp() * 1000))
        if df1m is not None and len(df1m) > 0:
            min_low = float(df1m["low"].min())
            if min_low <= resistance:
                log.info(f"IPTAL (erken gecersizlik): {symbol} | direnc={resistance:.6f} altina indi, "
                          f"onay penceresinde ({elapsed_min:.0f}dk) Telegram GONDERILMEDI.")
                del pending_signals[symbol]
                continue

        if elapsed_min >= WATCH_WINDOW_MIN:
            log.info(f"ONAYLANDI: {symbol} | {elapsed_min:.0f}dk boyunca direnc altina inmedi -> gonderiliyor.")
            confirmed.append(info["signal"])
            del pending_signals[symbol]
        # else: hala onay penceresinde, bir sonraki taramada tekrar kontrol edilecek

    return confirmed


# ══════════════════════════════════════════════════════════════════
# COOLDOWN (sadece FIILEN GONDERILEN sinyaller icin)
# ══════════════════════════════════════════════════════════════════
def should_send(symbol):
    now = time.time()
    if now - last_signal.get(symbol, 0) < SIGNAL_COOLDOWN:
        return False
    last_signal[symbol] = now
    return True


# ══════════════════════════════════════════════════════════════════
# TELEGRAM
# ══════════════════════════════════════════════════════════════════
def send_telegram(text):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log.error("TELEGRAM_TOKEN/TELEGRAM_CHAT_ID eksik, konsola yaziliyor:\n" + text)
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            data={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"},
            timeout=10,
        )
        return r.status_code == 200
    except Exception as e:
        log.error(f"Telegram hata: {e}")
        return False


def format_signal_message(signal: Signal):
    coin = signal.symbol.replace("USDT", "/USDT")
    sep = "=" * 24
    lines = [
        "🟢 <b>DİRENÇ KIRILIMI (ONAYLANMIŞ)</b>",
        sep,
        f"💱 <b>Coin:</b> {coin}",
        f"💰 <b>Referans Fiyat (kırılım kapanışı):</b> {signal.price:.6f}",
        f"📍 <b>Kırılan Direnç ({TIMEFRAME}, son {RES_LOOKBACK} mum):</b> {signal.resistance:.6f}",
        f"📏 <b>Kırılım Mesafesi:</b> %{signal.break_pct:.2f}",
        f"🕯️ <b>Mum Gövdesi:</b> %{signal.body_pct:.2f}",
        f"✅ <b>Onay:</b> {WATCH_WINDOW_MIN}dk boyunca direnç altına inmedi",
        sep,
        f"⏰ {datetime.now().strftime('%H:%M:%S %d/%m/%Y')}",
        "⚠️ Fiyat onay süresince değişmiş olabilir, güncel fiyatı kontrol et.",
    ]
    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════
# TARAMA DONGUSU (paralel, tum coinler) - ARTIK SADECE TESPIT + PENDING'E EKLEME
# ══════════════════════════════════════════════════════════════════
def check_signal(symbol, quote_volumes=None):
    try:
        signal = analyze_symbol(symbol, quote_volumes=quote_volumes)
        if signal is None:
            return None, {"symbol": symbol, "status": "no_signal"}
        return signal, {"symbol": symbol, "status": "detected"}
    except Exception as e:
        return None, {"symbol": symbol, "status": "error", "error": str(e)}


def run_scan_parallel():
    symbols = get_symbols()
    total = len(symbols)
    log.info(f"TARAMA BASLADI | Coin: {total} | Workers: {MAX_WORKERS} | TF: {TIMEFRAME} | "
              f"Direnc lookback: {RES_LOOKBACK} | Onay penceresi: {WATCH_WINDOW_MIN}dk")

    quote_volumes = get_all_24h_quote_volumes() if USE_LIQUIDITY_FILTER else {}

    stats = {"total": total, "detected": 0, "no_signal": 0, "error": 0, "already_pending_or_processed": 0}
    new_pending = 0

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(check_signal, s, quote_volumes): s for s in symbols}
        completed = 0
        for future in as_completed(futures):
            completed += 1
            try:
                signal, info = future.result()
                status = info["status"]
                stats[status] = stats.get(status, 0) + 1

                if status == "detected":
                    symbol = signal.symbol
                    # ayni 15dk mum daha once islendiyse (pending'de veya daha once
                    # onaylanip gonderildiyse) tekrar ekleme
                    if processed_bars.get(symbol) == signal.bar_time:
                        stats["already_pending_or_processed"] += 1
                        continue
                    if symbol in pending_signals:
                        stats["already_pending_or_processed"] += 1
                        continue
                    # not: cooldown kontrolu burada degil, fiilen gonderim aninda (should_send) yapilir

                    processed_bars[symbol] = signal.bar_time
                    pending_signals[symbol] = {
                        "signal": signal,
                        "resistance": signal.resistance,
                        "breakout_close_time": signal.breakout_close_time,
                        "detected_at": time.time(),
                    }
                    new_pending += 1
                    log.info(f"YENI ADAY: {symbol} | direnc={signal.resistance:.6f} | "
                              f"onay penceresine alindi ({WATCH_WINDOW_MIN}dk izlenecek)")

            except Exception as e:
                log.error(f"Future hata: {e}")
                stats["error"] += 1

            if completed % 100 == 0 or completed == total:
                log.info(f"[{completed}/{total}] Tespit:{stats['detected']} Yeni-aday:{new_pending} "
                          f"Hata:{stats['error']}")

    log.info(f"Tarama tamamlandi | {stats}")

    # --- bekleyen sinyalleri kontrol et (erken gecersizlik / onay) ---
    confirmed_signals = check_pending_confirmations()

    sent_count = 0
    for signal in confirmed_signals:
        if not should_send(signal.symbol):
            log.info(f"Cooldown nedeniyle atlandi (zaten yakin zamanda gonderilmis): {signal.symbol}")
            continue
        try:
            msg = format_signal_message(signal)
            if send_telegram(msg):
                sent_count += 1
                log.info(f"SINYAL GONDERILDI (onaylanmis): {signal.symbol} "
                          f"referans_fiyat={signal.price:.6f} direnc={signal.resistance:.6f}")
            else:
                log.error(f"Telegram gonderilemedi: {signal.symbol}")
        except Exception as e:
            log.error(f"Gonderim hatasi {signal.symbol}: {e}")

    log.info(f"Bu dongude gonderilen onayli sinyal: {sent_count} | "
              f"su an onay bekleyen: {len(pending_signals)}")
    return sent_count


# ══════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════
def main():
    log.info("=" * 60)
    log.info("15DK DIRENC KIRILIM SCANNER v2 (ONAY PENCERELI) baslatildi")
    log.info(f"Max coin       : {MAX_COINS}")
    log.info(f"Workers        : {MAX_WORKERS}")
    log.info(f"TF             : {TIMEFRAME}")
    log.info(f"Direnc lookback: {RES_LOOKBACK} mum")
    log.info(f"Min kirilim    : %{RES_BREAK_PCT}")
    log.info(f"Min mum govdesi: %{MIN_CANDLE_BODY_PCT}")
    log.info(f"Likidite filtre: {USE_LIQUIDITY_FILTER} (min {MIN_QUOTE_VOLUME_24H/1e6:.1f}M USDT)")
    log.info(f"Onay penceresi : {WATCH_WINDOW_MIN} dakika (1dk mumlarla erken gecersizlik kontrolu)")
    log.info(f"Tarama araligi : {SCAN_INTERVAL} sn")
    log.info(f"Cooldown       : {SIGNAL_COOLDOWN} sn")
    log.info("=" * 60)

    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log.error("TELEGRAM_TOKEN ve TELEGRAM_CHAT_ID eksik!")
        return

    send_telegram(
        "🚀 DIRENC KIRILIM SCANNER v2 (ONAY PENCERELI) BASLADI\n"
        + "=" * 30 + "\n"
        f"💱 TF: {TIMEFRAME}\n"
        f"📍 Direnc: son {RES_LOOKBACK} mumun en yuksegi\n"
        f"📏 Min kirilim: %{RES_BREAK_PCT}\n"
        f"🕯️ Min mum govdesi: %{MIN_CANDLE_BODY_PCT}\n"
        f"✅ Onay penceresi: {WATCH_WINDOW_MIN}dk (erken cokenler artik elenecek)\n"
        f"💧 Min likidite: {MIN_QUOTE_VOLUME_24H/1e6:.1f}M USDT\n"
        f"⏰ Cooldown: {SIGNAL_COOLDOWN}sn\n"
        f"⚡ Workers: {MAX_WORKERS}"
    )

    while True:
        try:
            run_scan_parallel()
        except Exception as e:
            log.error(f"run_scan genel hata: {e}")

        log.info(f"{SCAN_INTERVAL}sn bekleniyor... (onay bekleyen: {len(pending_signals)})")
        time.sleep(SCAN_INTERVAL)


if __name__ == "__main__":
    main()
