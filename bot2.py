# -*- coding: utf-8 -*-
"""
15DK DIRENC KIRILIM SCANNER BOT + 4H SMA100 MTF ONAY FILTRESI
- 15dk grafikte son N mumun en yuksegini (direnc) hesaplar (SADECE KAPANMIS mumlar - repaint yok)
- Kirilim mumu direnci gecmis, YESIL olmali, govdesi (open-close farki) en az MIN_CANDLE_BODY_PCT olmali
- Coin'in son 24s USDT hacmi MIN_QUOTE_VOLUME_24H altindaysa sinyal uretilmez (dusuk likidite elenir)
- YENI: 4H SMA100 MTF onayi -> 4h'de SMA100 kesisimi (crossover) son MAX_BARS_SINCE_CROSS_4H
  4h mum icinde olmus olmali VE fiyat hala SMA100 ustunde kalmis olmali. Boylece kesisim
  aninda gelen 15dk kirilimlar da, kesisimden 1-4 mum sonra gelenler de yakalanir.
- Kosullar saglaninca Telegram'a bildirim atar
"""
import os
import time
import logging
from datetime import datetime
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
RES_LOOKBACK = int(os.getenv("RES_LOOKBACK", "50"))          # direnc kac mumdan hesaplansin
RES_BREAK_PCT = float(os.getenv("RES_BREAK_PCT", "0.5"))     # direncten en az % kac yukarida kapanmali
MIN_CANDLE_BODY_PCT = float(os.getenv("MIN_CANDLE_BODY_PCT", "10.0"))  # kirilim mumunun govdesi min %

# --- LIKIDITE FILTRESI ---
USE_LIQUIDITY_FILTER = os.getenv("USE_LIQUIDITY_FILTER", "true").lower() == "true"
MIN_QUOTE_VOLUME_24H = float(os.getenv("MIN_QUOTE_VOLUME_24H", "5000000"))  # 5 milyon USDT

# --- YENI: 4H SMA100 MTF ONAY FILTRESI ---
USE_4H_SMA_FILTER = os.getenv("USE_4H_SMA_FILTER", "true").lower() == "true"
SMA_PERIOD_4H = int(os.getenv("SMA_PERIOD_4H", "100"))
MAX_BARS_SINCE_CROSS_4H = int(os.getenv("MAX_BARS_SINCE_CROSS_4H", "4"))  # kesisimden sonra kac 4h muma kadar gecerli

BINANCE_BASE = "https://fapi.binance.com"
last_signal = {}


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


def get_klines(symbol, interval, limit=200):
    try:
        session = requests.Session()
        r = session.get(
            f"{BINANCE_BASE}/fapi/v1/klines",
            params={"symbol": symbol, "interval": interval, "limit": limit},
            timeout=REQUEST_TIMEOUT,
        )
        raw = r.json()
        df = pd.DataFrame(raw, columns=[
            "open_time", "open", "high", "low", "close", "volume", "close_time",
            "qav", "trades", "tbv", "tqv", "ignore"])
        for c in ["open", "high", "low", "close", "volume"]:
            df[c] = df[c].astype(float)
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
    """
    TUM sembollerin 24s USDT hacmini TEK istekte ceker. Boylece her coin icin
    ayri istek atmaya gerek kalmaz, tarama suresine ek yuk binmez.
    Dondugu deger: {symbol: quoteVolume} sozlugu.
    """
    try:
        session = requests.Session()
        r = session.get(f"{BINANCE_BASE}/fapi/v1/ticker/24hr", timeout=15)
        data = r.json()
        return {d["symbol"]: float(d.get("quoteVolume", 0)) for d in data}
    except Exception as e:
        log.error(f"get_all_24h_quote_volumes hata: {e}")
        return {}


# ══════════════════════════════════════════════════════════════════
# YENI: 4H SMA100 MTF ONAY KONTROLU
# ══════════════════════════════════════════════════════════════════
def check_4h_sma_confirmation(symbol):
    """
    4h'de SMA100 kesisimi (crossover) son MAX_BARS_SINCE_CROSS_4H 4h mum icinde
    gerceklesmis mi VE fiyat hala SMA100 ustunde mi kontrol eder.

    - Kesisim mumunun kendisinde de, 1-4 mum sonrasinda da (fiyat hala ustteyse) True doner.
    - Kesisimden sonra pencere disina tasmissa (5+ mum gecmisse) -> False.
    - Kesisim olmus ama fiyat tekrar SMA100 altina dusmusse -> False (fake kesisim sayilir).

    Sadece KAPANMIS 4h mumlar kullanilir, repaint yoktur.
    """
    limit = SMA_PERIOD_4H + MAX_BARS_SINCE_CROSS_4H + 10
    df = get_klines_closed(symbol, "4h", limit=limit)
    if df is None or len(df) < SMA_PERIOD_4H + 2:
        return False

    df["sma100_4h"] = df["close"].rolling(SMA_PERIOD_4H).mean()
    df = df.dropna(subset=["sma100_4h"]).reset_index(drop=True)
    if len(df) < 2:
        return False

    current_close = float(df["close"].iloc[-1])
    current_sma = float(df["sma100_4h"].iloc[-1])

    # Sart 1: fiyat su an SMA100'un ustunde olmali
    if current_close <= current_sma:
        return False

    # Sart 2: pencere icinde (son MAX_BARS_SINCE_CROSS_4H mum) bir yukari kesisim olmus olmali
    n = len(df)
    start_idx = max(1, n - MAX_BARS_SINCE_CROSS_4H)
    for i in range(n - 1, start_idx - 1, -1):
        prev_close = float(df["close"].iloc[i - 1])
        prev_sma = float(df["sma100_4h"].iloc[i - 1])
        cur_close = float(df["close"].iloc[i])
        cur_sma = float(df["sma100_4h"].iloc[i])
        if prev_close <= prev_sma and cur_close > cur_sma:
            return True

    return False


# ══════════════════════════════════════════════════════════════════
# SINYAL: DIRENC KIRILIMI
# ══════════════════════════════════════════════════════════════════
@dataclass
class Signal:
    symbol: str
    price: float
    resistance: float
    break_pct: float
    body_pct: float
    bar_time: str
    sma4h_onay: bool = False


def analyze_symbol(symbol, quote_volumes=None):
    # --- likidite kontrolu once yapilir, dusukse hic kline cekmeye gerek yok ---
    if USE_LIQUIDITY_FILTER:
        qv = (quote_volumes or {}).get(symbol, 0.0)
        if qv < MIN_QUOTE_VOLUME_24H:
            return None

    # RES_LOOKBACK (direnc icin gecmis mum) + 1 (kirilim mumunun kendisi) + biraz pay
    df = get_klines_closed(symbol, TIMEFRAME, limit=RES_LOOKBACK + 5)
    if df is None or len(df) < RES_LOOKBACK + 1:
        return None

    breakout = df.iloc[-1]                              # kirilim adayi mum
    history = df.iloc[-(RES_LOOKBACK + 1):-1]            # direnc SADECE bu mumlardan hesaplanir (kirilim mumu HARIC)

    resistance = float(history["high"].max())
    open_now = float(breakout["open"])
    close_now = float(breakout["close"])
    bar_time = str(breakout["open_time"])

    if close_now <= open_now:
        return None  # yesil mum degil -> kirilim sayilmaz

    if close_now <= resistance:
        return None  # direnc kirilmamis

    break_pct = (close_now - resistance) / resistance * 100
    body_pct = (close_now - open_now) / open_now * 100

    if break_pct < RES_BREAK_PCT:
        return None
    if body_pct < MIN_CANDLE_BODY_PCT:
        return None

    # --- YENI: 4H SMA100 MTF onayi -- sadece 15dk sartlarini gecen adaylar icin kontrol edilir
    # (her coin icin degil, sadece kirilim uretenler icin -- gereksiz API yukunu onler)
    sma4h_onay = True
    if USE_4H_SMA_FILTER:
        sma4h_onay = check_4h_sma_confirmation(symbol)
        if not sma4h_onay:
            return None

    return Signal(
        symbol=symbol, price=close_now, resistance=resistance,
        break_pct=break_pct, body_pct=body_pct, bar_time=bar_time,
        sma4h_onay=sma4h_onay,
    )


# ══════════════════════════════════════════════════════════════════
# COOLDOWN
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
        "🟢 <b>DİRENÇ KIRILIMI</b>",
        sep,
        f"💱 <b>Coin:</b> {coin}",
        f"💰 <b>Fiyat:</b> {signal.price:.6f}",
        f"📍 <b>Kırılan Direnç ({TIMEFRAME}, son {RES_LOOKBACK} mum):</b> {signal.resistance:.6f}",
        f"📏 <b>Kırılım Mesafesi:</b> %{signal.break_pct:.2f}",
        f"🕯️ <b>Mum Gövdesi:</b> %{signal.body_pct:.2f}",
        f"📊 <b>4H SMA100 Onayı:</b> {'✅ Var (son ' + str(MAX_BARS_SINCE_CROSS_4H) + ' mum içinde kesişim)' if signal.sma4h_onay else '—'}",
        sep,
        f"⏰ {datetime.now().strftime('%H:%M:%S %d/%m/%Y')}",
    ]
    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════
# TARAMA DONGUSU (paralel, tum coinler)
# ══════════════════════════════════════════════════════════════════
def check_signal(symbol, quote_volumes=None):
    try:
        signal = analyze_symbol(symbol, quote_volumes=quote_volumes)
        if signal is None:
            return None, {"symbol": symbol, "status": "no_signal"}
        if not should_send(symbol):
            return None, {"symbol": symbol, "status": "cooldown"}
        return signal, {"symbol": symbol, "status": "signal"}
    except Exception as e:
        return None, {"symbol": symbol, "status": "error", "error": str(e)}


def run_scan_parallel():
    symbols = get_symbols()
    total = len(symbols)
    log.info(f"TARAMA BASLADI | Coin: {total} | Workers: {MAX_WORKERS} | TF: {TIMEFRAME} | "
              f"Direnc lookback: {RES_LOOKBACK} | 4H SMA filtre: {USE_4H_SMA_FILTER} "
              f"(pencere: {MAX_BARS_SINCE_CROSS_4H} mum)")

    # likidite verisi TEK istekte, tarama basinda bir kez cekilir (600 ayri istek yerine 1 istek)
    quote_volumes = get_all_24h_quote_volumes() if USE_LIQUIDITY_FILTER else {}

    stats = {"total": total, "signal": 0, "no_signal": 0, "cooldown": 0, "error": 0}
    found = []

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(check_signal, s, quote_volumes): s for s in symbols}
        completed = 0
        for future in as_completed(futures):
            completed += 1
            try:
                signal, info = future.result()
                status = info["status"]
                stats[status] = stats.get(status, 0) + 1
                if status == "signal":
                    found.append(signal)
            except Exception as e:
                log.error(f"Future hata: {e}")
                stats["error"] += 1

            if completed % 100 == 0 or completed == total:
                log.info(f"[{completed}/{total}] Sinyal:{stats['signal']} NoSignal:{stats['no_signal']} Hata:{stats['error']}")

    for signal in found:
        try:
            msg = format_signal_message(signal)
            if send_telegram(msg):
                log.info(f"SINYAL GONDERILDI: {signal.symbol} fiyat={signal.price:.6f} direnc={signal.resistance:.6f}")
            else:
                log.error(f"Telegram gonderilemedi: {signal.symbol}")
        except Exception as e:
            log.error(f"Gonderim hatasi {signal.symbol}: {e}")

    log.info(f"Tarama tamamlandi | {stats['signal']} sinyal | {stats}")
    return stats["signal"]


# ══════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════
def main():
    log.info("=" * 60)
    log.info("15DK DIRENC KIRILIM SCANNER + 4H SMA100 FILTRESI baslatildi")
    log.info(f"Max coin       : {MAX_COINS}")
    log.info(f"Workers        : {MAX_WORKERS}")
    log.info(f"TF             : {TIMEFRAME}")
    log.info(f"Direnc lookback: {RES_LOOKBACK} mum")
    log.info(f"Min kirilim    : %{RES_BREAK_PCT}")
    log.info(f"Min mum govdesi: %{MIN_CANDLE_BODY_PCT}")
    log.info(f"Likidite filtre: {USE_LIQUIDITY_FILTER} (min {MIN_QUOTE_VOLUME_24H/1e6:.1f}M USDT)")
    log.info(f"4H SMA filtre  : {USE_4H_SMA_FILTER} (SMA{SMA_PERIOD_4H}, pencere {MAX_BARS_SINCE_CROSS_4H} mum)")
    log.info(f"Tarama araligi : {SCAN_INTERVAL} sn")
    log.info(f"Cooldown       : {SIGNAL_COOLDOWN} sn")
    log.info("=" * 60)

    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log.error("TELEGRAM_TOKEN ve TELEGRAM_CHAT_ID eksik!")
        return

    send_telegram(
        "🚀 DIRENC KIRILIM SCANNER + 4H SMA100 FILTRESI BASLADI\n"
        + "=" * 30 + "\n"
        f"💱 TF: {TIMEFRAME}\n"
        f"📍 Direnc: son {RES_LOOKBACK} mumun en yuksegi\n"
        f"📏 Min kirilim: %{RES_BREAK_PCT}\n"
        f"🕯️ Min mum govdesi: %{MIN_CANDLE_BODY_PCT}\n"
        f"💧 Min likidite: {MIN_QUOTE_VOLUME_24H/1e6:.1f}M USDT\n"
        f"📊 4H SMA100 onay penceresi: {MAX_BARS_SINCE_CROSS_4H} mum\n"
        f"⏰ Cooldown: {SIGNAL_COOLDOWN}sn\n"
        f"⚡ Workers: {MAX_WORKERS}"
    )

    while True:
        try:
            run_scan_parallel()
        except Exception as e:
            log.error(f"run_scan genel hata: {e}")

        log.info(f"{SCAN_INTERVAL}sn bekleniyor...")
        time.sleep(SCAN_INTERVAL)


if __name__ == "__main__":
    main()
