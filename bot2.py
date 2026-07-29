# -*- coding: utf-8 -*-
"""
15DK DIRENC KIRILIM SCANNER BOT - v2 (MOMENTUM TEYIT KATMANLARI EKLENMIS)

YENI EKLENENLER (v1'e gore):
  1) HACIM TEYIDI       - kirilim mumu ortalama hacmin X katindan fazla olmali
  2) UST ZD TREND FILTRESI - 1h/4h EMA uzerinde olmali (trend yonuyle uyumlu kirilim)
  3) LIKIDITE FILTRESI  - 24s USDT hacmi cok dusuk coinler elenir (whipsaw riski)
  4) ATR BAZLI KIRILIM MESAFESI - sabit % yerine volatiliteye gore normalize kirilim
  5) SIKISMA (BB WIDTH) FILTRESI - kirilim oncesi range daralmissa bonus puan
  6) SKOR SISTEMI       - her filtre puan katkisi yapar, MIN_SCORE altindaki sinyaller elenir
  7) SONUC TAKIP        - her sinyali kaydeder, N dakika sonra fiyati tekrar cekip
                          "devam etti mi / devam etmedi mi" olarak loglar (signal_results.csv)
                          -> Hangi filtre gercekten ise yariyor, zamanla bu veriden gorulur.

Not: Btm filtreleri ac/kapat env degiskenleriyle yapabilirsin, ustteki v1'i bozmadan
     kademeli olarak devreye alabilirsin (once sadece hacim filtresini ac, 1 hafta test et, vb.)
"""
import os
import csv
import time
import logging
import threading
from datetime import datetime
from dataclasses import dataclass, field
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
import pandas as pd
import numpy as np

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

# --- YENI FILTRELER (hepsi ac/kapat edilebilir) ---
USE_VOLUME_FILTER = os.getenv("USE_VOLUME_FILTER", "true").lower() == "true"
VOLUME_LOOKBACK = int(os.getenv("VOLUME_LOOKBACK", "20"))          # ortalama hacim kac mumdan
VOLUME_MULT_MIN = float(os.getenv("VOLUME_MULT_MIN", "1.5"))      # kirilim hacmi >= ortalama * bu katsayi

USE_HTF_TREND_FILTER = os.getenv("USE_HTF_TREND_FILTER", "true").lower() == "true"
HTF_TIMEFRAME = os.getenv("HTF_TIMEFRAME", "1h")
HTF_EMA_PERIOD = int(os.getenv("HTF_EMA_PERIOD", "50"))

USE_LIQUIDITY_FILTER = os.getenv("USE_LIQUIDITY_FILTER", "true").lower() == "true"
MIN_QUOTE_VOLUME_24H = float(os.getenv("MIN_QUOTE_VOLUME_24H", "5000000"))  # 5M USDT

USE_ATR_FILTER = os.getenv("USE_ATR_FILTER", "true").lower() == "true"
ATR_PERIOD = int(os.getenv("ATR_PERIOD", "14"))
ATR_BREAK_MULT_MIN = float(os.getenv("ATR_BREAK_MULT_MIN", "0.3"))  # kirilim mesafesi >= ATR * bu katsayi

USE_SQUEEZE_BONUS = os.getenv("USE_SQUEEZE_BONUS", "true").lower() == "true"
BB_PERIOD = int(os.getenv("BB_PERIOD", "20"))
BB_SQUEEZE_LOOKBACK = int(os.getenv("BB_SQUEEZE_LOOKBACK", "50"))   # BB genisligi bu periyodun en dusuk %25'i mi

MIN_SCORE = int(os.getenv("MIN_SCORE", "3"))   # toplam skor bu esigin altindaysa sinyal elenir

# --- SONUC TAKIP AYARLARI ---
TRACK_RESULTS = os.getenv("TRACK_RESULTS", "true").lower() == "true"
TRACK_MINUTES_LATER = int(os.getenv("TRACK_MINUTES_LATER", "60"))  # kac dk sonra sonuc kontrol edilsin
RESULTS_CSV = os.getenv("RESULTS_CSV", "signal_results.csv")

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
    TUM sembollerin 24s hacmini TEK istekte ceker (parametre verilmezse
    Binance hepsini doner). Boylece her aday coin icin ayri istek atmaya
    gerek kalmaz -> tarama suresine pratikte hic yuk binmez.
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


def get_last_price(symbol):
    try:
        session = requests.Session()
        r = session.get(
            f"{BINANCE_BASE}/fapi/v1/ticker/price",
            params={"symbol": symbol},
            timeout=REQUEST_TIMEOUT,
        )
        return float(r.json()["price"])
    except Exception:
        return None


# ══════════════════════════════════════════════════════════════════
# YARDIMCI GOSTERGELER
# ══════════════════════════════════════════════════════════════════
def calc_ema(series, period):
    return series.ewm(span=period, adjust=False).mean()


def calc_atr(df, period):
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.rolling(period).mean()


def calc_bb_width(df, period):
    close = df["close"]
    ma = close.rolling(period).mean()
    std = close.rolling(period).std()
    upper = ma + 2 * std
    lower = ma - 2 * std
    width = (upper - lower) / ma
    return width


# ══════════════════════════════════════════════════════════════════
# SINYAL: DIRENC KIRILIMI + MOMENTUM TEYIT SKORU
# ══════════════════════════════════════════════════════════════════
@dataclass
class Signal:
    symbol: str
    price: float
    resistance: float
    break_pct: float
    body_pct: float
    bar_time: str
    score: int
    reasons: list = field(default_factory=list)


def analyze_symbol(symbol, quote_volumes=None):
    # --- Temel kirilim kontrolu (v1 mantigi) ---
    df = get_klines_closed(symbol, TIMEFRAME, limit=max(RES_LOOKBACK + 5, BB_SQUEEZE_LOOKBACK + BB_PERIOD + 5))
    if df is None or len(df) < RES_LOOKBACK + 1:
        return None

    breakout = df.iloc[-1]
    history = df.iloc[-(RES_LOOKBACK + 1):-1]

    resistance = float(history["high"].max())
    open_now = float(breakout["open"])
    close_now = float(breakout["close"])
    bar_time = str(breakout["open_time"])

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

    # --- Skor sistemi: her teyit filtresi puan katar, elemeyen ama zorunlu olanlar direkt eler ---
    score = 0
    reasons = []

    # 1) Likidite filtresi (eleyici) - onbelleklenmis toplu veriden okur, ekstra istek atmaz
    if USE_LIQUIDITY_FILTER:
        qv = (quote_volumes or {}).get(symbol, 0.0)
        if qv < MIN_QUOTE_VOLUME_24H:
            return None
        reasons.append(f"likidite_ok({qv/1e6:.1f}M)")

    # 2) Hacim teyidi (puanli)
    if USE_VOLUME_FILTER:
        vol_hist = df["volume"].iloc[-(VOLUME_LOOKBACK + 1):-1]
        avg_vol = float(vol_hist.mean()) if len(vol_hist) else 0
        breakout_vol = float(breakout["volume"])
        if avg_vol > 0 and breakout_vol >= avg_vol * VOLUME_MULT_MIN:
            score += 2
            reasons.append(f"hacim_teyit(x{breakout_vol/avg_vol:.1f})")
        else:
            # hacim yoksa kirilim guvenilirligi dusuk ama direkt eleme, sadece puan vermiyoruz
            reasons.append("hacim_zayif")

    # 3) Ust zaman dilimi trend filtresi (eleyici)
    if USE_HTF_TREND_FILTER:
        htf_df = get_klines_closed(symbol, HTF_TIMEFRAME, limit=HTF_EMA_PERIOD + 5)
        if htf_df is None or len(htf_df) < HTF_EMA_PERIOD:
            return None
        htf_ema = calc_ema(htf_df["close"], HTF_EMA_PERIOD).iloc[-1]
        htf_last_close = float(htf_df["close"].iloc[-1])
        if htf_last_close < htf_ema:
            return None  # ust zd dusus trendinde ise kirilimi alma
        score += 2
        reasons.append(f"htf_trend_ok({HTF_TIMEFRAME})")

    # 4) ATR bazli kirilim mesafesi (puanli)
    if USE_ATR_FILTER:
        atr = calc_atr(df, ATR_PERIOD).iloc[-1]
        if pd.notna(atr) and atr > 0:
            break_distance = close_now - resistance
            if break_distance >= atr * ATR_BREAK_MULT_MIN:
                score += 1
                reasons.append(f"atr_yeterli({break_distance/atr:.2f}xATR)")
            else:
                reasons.append("atr_zayif")

    # 5) Sikisma bonusu (puanli)
    if USE_SQUEEZE_BONUS:
        bb_width = calc_bb_width(df, BB_PERIOD)
        recent_width = bb_width.iloc[-(BB_SQUEEZE_LOOKBACK + 1):-1]
        current_width_ref = bb_width.iloc[-2]  # kirilimdan hemen onceki mumun genisligi
        if len(recent_width.dropna()) >= 10 and pd.notna(current_width_ref):
            percentile_25 = recent_width.quantile(0.25)
            if current_width_ref <= percentile_25:
                score += 1
                reasons.append("sikismadan_cikti")

    if score < MIN_SCORE:
        return None

    return Signal(
        symbol=symbol, price=close_now, resistance=resistance,
        break_pct=break_pct, body_pct=body_pct, bar_time=bar_time,
        score=score, reasons=reasons,
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
    reasons_txt = "\n".join(f"  • {r}" for r in signal.reasons)
    lines = [
        "🟢 <b>DİRENÇ KIRILIMI (Skor: {}/{})</b>".format(signal.score, MIN_SCORE),
        sep,
        f"💱 <b>Coin:</b> {coin}",
        f"💰 <b>Fiyat:</b> {signal.price:.6f}",
        f"📍 <b>Kırılan Direnç ({TIMEFRAME}, son {RES_LOOKBACK} mum):</b> {signal.resistance:.6f}",
        f"📏 <b>Kırılım Mesafesi:</b> %{signal.break_pct:.2f}",
        f"🕯️ <b>Mum Gövdesi:</b> %{signal.body_pct:.2f}",
        sep,
        "<b>Teyit Detayları:</b>",
        reasons_txt,
        sep,
        f"⏰ {datetime.now().strftime('%H:%M:%S %d/%m/%Y')}",
    ]
    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════
# SONUC TAKIP MEKANIZMASI
# Her sinyali kaydeder, TRACK_MINUTES_LATER dk sonra fiyati tekrar
# cekip devam etmis mi (break_pct'ten daha yukarida mi) diye loglar.
# Bu CSV zamanla "hangi teyit kombinasyonu gercekten isliyor" sorusunun
# gercek cevabini verir - filtreleri buna gore ayarla.
# ══════════════════════════════════════════════════════════════════
def ensure_results_csv():
    if not os.path.exists(RESULTS_CSV):
        with open(RESULTS_CSV, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow([
                "signal_time", "symbol", "score", "reasons",
                "signal_price", "resistance", "break_pct",
                f"price_after_{TRACK_MINUTES_LATER}m", "result_pct_change", "momentum_devam_etti"
            ])


def track_signal_result(signal: Signal):
    """Arka planda bekleyip sonucu CSV'ye yazan thread fonksiyonu."""
    time.sleep(TRACK_MINUTES_LATER * 60)
    later_price = get_last_price(signal.symbol)
    if later_price is None:
        return
    pct_change = (later_price - signal.price) / signal.price * 100
    # basit tanim: sinyal fiyatinin en az %0 uzerinde kalmissa "devam etti" say
    # (istersen daha siki bir esik: pct_change > break_pct * 0.5 gibi)
    continued = pct_change > 0
    ensure_results_csv()
    with open(RESULTS_CSV, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"), signal.symbol, signal.score,
            "|".join(signal.reasons), f"{signal.price:.6f}", f"{signal.resistance:.6f}",
            f"{signal.break_pct:.2f}", f"{later_price:.6f}", f"{pct_change:.2f}", continued,
        ])
    log.info(f"SONUC KAYDEDILDI: {signal.symbol} {TRACK_MINUTES_LATER}dk sonra %{pct_change:.2f} "
              f"({'DEVAM ETTI' if continued else 'DEVAM ETMEDI'})")


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
             f"Direnc lookback: {RES_LOOKBACK} | MIN_SCORE: {MIN_SCORE}")

    # Likidite verisi TEK istekte, tarama basinda bir kez cekilir (600 ayri istek yerine 1 istek)
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
                log.info(f"SINYAL GONDERILDI: {signal.symbol} skor={signal.score} fiyat={signal.price:.6f} direnc={signal.resistance:.6f}")
            else:
                log.error(f"Telegram gonderilemedi: {signal.symbol}")

            if TRACK_RESULTS:
                t = threading.Thread(target=track_signal_result, args=(signal,), daemon=True)
                t.start()
        except Exception as e:
            log.error(f"Gonderim hatasi {signal.symbol}: {e}")

    log.info(f"Tarama tamamlandi | {stats['signal']} sinyal | {stats}")
    return stats["signal"]


# ══════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════
def main():
    log.info("=" * 60)
    log.info("15DK DIRENC KIRILIM SCANNER v2 (momentum teyitli) baslatildi")
    log.info(f"Max coin       : {MAX_COINS}")
    log.info(f"Workers        : {MAX_WORKERS}")
    log.info(f"TF             : {TIMEFRAME}")
    log.info(f"Direnc lookback: {RES_LOOKBACK} mum")
    log.info(f"Min kirilim    : %{RES_BREAK_PCT}")
    log.info(f"Min mum govdesi: %{MIN_CANDLE_BODY_PCT}")
    log.info(f"Hacim filtresi : {USE_VOLUME_FILTER} (x{VOLUME_MULT_MIN})")
    log.info(f"HTF trend      : {USE_HTF_TREND_FILTER} ({HTF_TIMEFRAME}, EMA{HTF_EMA_PERIOD})")
    log.info(f"Likidite filtre: {USE_LIQUIDITY_FILTER} (min {MIN_QUOTE_VOLUME_24H/1e6:.1f}M USDT)")
    log.info(f"ATR filtresi   : {USE_ATR_FILTER} (x{ATR_BREAK_MULT_MIN})")
    log.info(f"Sikisma bonusu : {USE_SQUEEZE_BONUS}")
    log.info(f"MIN_SCORE      : {MIN_SCORE}")
    log.info(f"Sonuc takip    : {TRACK_RESULTS} ({TRACK_MINUTES_LATER}dk sonra -> {RESULTS_CSV})")
    log.info(f"Tarama araligi : {SCAN_INTERVAL} sn")
    log.info(f"Cooldown       : {SIGNAL_COOLDOWN} sn")
    log.info("=" * 60)

    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log.error("TELEGRAM_TOKEN ve TELEGRAM_CHAT_ID eksik!")
        return

    if TRACK_RESULTS:
        ensure_results_csv()

    send_telegram(
        "🚀 DIRENC KIRILIM SCANNER v2 (MOMENTUM TEYITLI) BASLADI\n"
        + "=" * 30 + "\n"
        f"💱 TF: {TIMEFRAME}\n"
        f"📍 Direnc: son {RES_LOOKBACK} mumun en yuksegi\n"
        f"📏 Min kirilim: %{RES_BREAK_PCT}\n"
        f"🕯️ Min mum govdesi: %{MIN_CANDLE_BODY_PCT}\n"
        f"✅ Aktif filtreler: Hacim={USE_VOLUME_FILTER} HTF={USE_HTF_TREND_FILTER} "
        f"Likidite={USE_LIQUIDITY_FILTER} ATR={USE_ATR_FILTER} Sikisma={USE_SQUEEZE_BONUS}\n"
        f"🎯 Min skor: {MIN_SCORE}\n"
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
