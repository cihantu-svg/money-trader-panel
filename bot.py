# -*- coding: utf-8 -*-
"""
5DK MAJOR LEVEL (SMA100) KIRILIM SCANNER BOT
─────────────────────────────────────────────
STRATEJI:
  1) Major Level = SMA(close, 100)  → Pine Script'teki "MONEY TRADER - FULL PAKET"
     indikatöründeki majorLevel = ta.sma(close, major_line_len) hesabının birebir
     Python karşılığı (sadece bu hesap alındı, başka hiçbir şeye dokunulmadı).
  2) Coin, kırılım mumundan hemen önceki en az MIN_BARS_BELOW (varsayılan 75) mum
     boyunca KESINTISIZ olarak kendi Major Level'inin (SMA100) ALTINDA kalmış olmalı.
  3) Kırılım mumu kapanışı Major Level'i en az MAJOR_BREAK_PCT (varsayılan %5) ile
     YUKARI kırmış olmalı (close > majorLevel * (1 + %5)).
  4) Likidite/işlem girişi filtresi: coin'in son 24s USDT hacmi
     MIN_QUOTE_VOLUME_24H (varsayılan 3.000.000 USDT) altındaysa sinyal üretilmez.

  Binance tarama altyapısı (get_symbols, get_klines, get_klines_closed,
  get_all_24h_quote_volumes, ThreadPoolExecutor ile paralel tarama, cooldown
  mekanizması) ve Telegram bildirim ayarları/fonksiyonları, mevcut GitHub
  projenizdeki (15dk direnç kırılım scanner) yapıdan SADECE bu kısımlar
  alınarak buraya taşındı. Direnç kırılımı / para takibi (taker-buy) mantığının
  bu stratejiyle hiçbir bağlantısı yoktur, dahil edilmedi.

NOT (varsayım): "min 3 milyon işlem girişi" koşulunu, mevcut projenizdeki
likidite filtresiyle aynı mantıkta -> coin'in 24 saatlik USDT hacmi olarak
yorumladım (eşik 3.000.000 USDT). Eğer kastınız kırılım mumunun kendi hacmi
ise MIN_QUOTE_VOLUME_24H yerine "breakout mumu hacmi" kontrolüne kolayca
çevrilebilir, tek satır değişir.
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

TIMEFRAME = os.getenv("TIMEFRAME", "5m")

# --- MAJOR LEVEL (SMA100) STRATEJI AYARLARI ---
MAJOR_SMA_LEN = int(os.getenv("MAJOR_SMA_LEN", "100"))          # Pine'daki major_line_len
MIN_BARS_BELOW = int(os.getenv("MIN_BARS_BELOW", "75"))         # en az kac bar altinda kalmis olmali
MAJOR_BREAK_PCT = float(os.getenv("MAJOR_BREAK_PCT", "5.0"))    # major_break_pct - min kirilim yuzdesi

# --- LIKIDITE / ISLEM GIRISI FILTRESI ---
USE_LIQUIDITY_FILTER = os.getenv("USE_LIQUIDITY_FILTER", "true").lower() == "true"
MIN_QUOTE_VOLUME_24H = float(os.getenv("MIN_QUOTE_VOLUME_24H", "3000000"))  # 3 milyon USDT

BINANCE_BASE = "https://fapi.binance.com"
last_signal = {}


# ══════════════════════════════════════════════════════════════════
# BINANCE VERI CEKME (mevcut GitHub projesinden alinan tarama altyapisi)
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
    Tum sembollerin 24s USDT hacmini TEK istekte ceker.
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
# MAJOR LEVEL (SMA100) HESABI
# Pine Script'teki: majorLevel = ta.sma(close, major_line_len)
# ══════════════════════════════════════════════════════════════════
def add_major_level(df, length=MAJOR_SMA_LEN):
    df = df.copy()
    df["majorLevel"] = df["close"].rolling(length).mean()
    return df


# ══════════════════════════════════════════════════════════════════
# SINYAL: 75+ BAR MAJOR ALTINDA KALIP MIN %5 ILE KIRILIM
# ══════════════════════════════════════════════════════════════════
@dataclass
class Signal:
    symbol: str
    price: float
    major_level: float
    break_pct: float
    bars_below: int
    bar_time: str
    quote_volume_24h: float


def analyze_symbol(symbol, quote_volumes=None):
    # --- likidite / islem girisi kontrolu once yapilir ---
    qv = (quote_volumes or {}).get(symbol, 0.0)
    if USE_LIQUIDITY_FILTER and qv < MIN_QUOTE_VOLUME_24H:
        return None

    needed = MAJOR_SMA_LEN + MIN_BARS_BELOW + 5
    df = get_klines_closed(symbol, TIMEFRAME, limit=needed)
    if df is None or len(df) < needed:
        return None

    df = add_major_level(df, MAJOR_SMA_LEN)
    df = df.dropna(subset=["majorLevel"]).reset_index(drop=True)
    if len(df) < MIN_BARS_BELOW + 1:
        return None

    breakout = df.iloc[-1]           # kirilim adayi (son kapanmis) mum
    close_now = float(breakout["close"])
    major_now = float(breakout["majorLevel"])
    bar_time = str(breakout["open_time"])

    if major_now <= 0 or close_now <= major_now:
        return None  # major level'in ustunde kapanmamis -> kirilim yok

    break_pct = (close_now - major_now) / major_now * 100
    if break_pct < MAJOR_BREAK_PCT:
        return None  # min %5 kirilim sarti saglanmadi

    # kirilim mumu HARIC, hemen oncesindeki MIN_BARS_BELOW mum
    history = df.iloc[-(MIN_BARS_BELOW + 1):-1]
    below_mask = history["close"] < history["majorLevel"]
    if not below_mask.all():
        return None  # kesintisiz "altinda kalmis" sarti bozulmus

    return Signal(
        symbol=symbol,
        price=close_now,
        major_level=major_now,
        break_pct=break_pct,
        bars_below=len(history),
        bar_time=bar_time,
        quote_volume_24h=qv,
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
# TELEGRAM (mevcut GitHub projesinden alinan bildirim yapisi)
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
        "🟠 <b>MAJOR LEVEL (SMA100) KIRILIMI</b>",
        sep,
        f"💱 <b>Coin:</b> {coin}",
        f"💰 <b>Fiyat:</b> {signal.price:.6f}",
        f"📈 <b>Major Level (SMA{MAJOR_SMA_LEN}, {TIMEFRAME}):</b> {signal.major_level:.6f}",
        f"📏 <b>Kırılım Mesafesi:</b> %{signal.break_pct:.2f}",
        f"⏳ <b>Öncesinde Altında Kaldığı Bar:</b> {signal.bars_below} (min {MIN_BARS_BELOW})",
        f"💧 <b>24s Hacim:</b> {signal.quote_volume_24h:,.0f} USDT (min {MIN_QUOTE_VOLUME_24H:,.0f})",
    ]
    lines += [sep, f"⏰ {datetime.now().strftime('%H:%M:%S %d/%m/%Y')}"]
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
              f"SMA{MAJOR_SMA_LEN} | min {MIN_BARS_BELOW} bar altinda | min %{MAJOR_BREAK_PCT} kirilim")

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
                log.info(f"SINYAL GONDERILDI: {signal.symbol} fiyat={signal.price:.6f} major={signal.major_level:.6f}")
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
    log.info("5DK MAJOR LEVEL (SMA100) KIRILIM SCANNER baslatildi")
    log.info(f"Max coin        : {MAX_COINS}")
    log.info(f"Workers         : {MAX_WORKERS}")
    log.info(f"TF              : {TIMEFRAME}")
    log.info(f"Major SMA       : {MAJOR_SMA_LEN}")
    log.info(f"Min alt bar     : {MIN_BARS_BELOW}")
    log.info(f"Min kirilim     : %{MAJOR_BREAK_PCT}")
    log.info(f"Likidite filtre : {USE_LIQUIDITY_FILTER} (min {MIN_QUOTE_VOLUME_24H/1e6:.1f}M USDT)")
    log.info(f"Tarama araligi  : {SCAN_INTERVAL} sn")
    log.info(f"Cooldown        : {SIGNAL_COOLDOWN} sn")
    log.info("=" * 60)

    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log.error("TELEGRAM_TOKEN ve TELEGRAM_CHAT_ID eksik!")
        return

    send_telegram(
        "🚀 MAJOR LEVEL (SMA100) KIRILIM SCANNER BASLADI\n"
        + "=" * 30 + "\n"
        f"💱 TF: {TIMEFRAME}\n"
        f"📈 Major Level: SMA{MAJOR_SMA_LEN}\n"
        f"⏳ Min altinda kalma: {MIN_BARS_BELOW} bar\n"
        f"📏 Min kirilim: %{MAJOR_BREAK_PCT}\n"
        f"💧 Min islem girisi (24s hacim): {MIN_QUOTE_VOLUME_24H/1e6:.1f}M USDT\n"
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
