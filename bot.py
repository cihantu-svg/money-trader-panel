# -*- coding: utf-8 -*-
"""
15DK DIRENC KIRILIM SCANNER BOT
- 15dk grafikte son N mumun en yuksegini (direnc) hesaplar (SADECE KAPANMIS mumlar - repaint yok)
- Kirilim mumu direnci gecmis, YESIL olmali, govdesi (open-close farki) en az MIN_CANDLE_BODY_PCT olmali
- Coin'in son 24s USDT hacmi MIN_QUOTE_VOLUME_24H altindaysa sinyal uretilmez (dusuk likidite elenir)
- YENI: PARA TAKIBI (ORDER FLOW / MONEY FLOW) ONAYI
    1) Kirilim mumunda taker-buy orani MIN_TAKER_BUY_RATIO ustunde olmali (agresif alici hacmi baskin mi?)
    2) Kirilim ONCESI son AVG_TAKER_LOOKBACK mumun ortalama taker-buy orani MIN_AVG_TAKER_BUY_RATIO ustunde
       olmali -> bu, kirilimdan once zaten para girisi basladigini (trend devaminin habercisi) dogrular.
       Rastgele/anlik hacim patlamasiyla, biriken gercek alim baskisini bu sekilde ayirt ediyoruz.
    3) (Opsiyonel, varsayilan kapali) MFI (Money Flow Index) filtresi - klasik ikinci onay katmani
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
RES_LOOKBACK = int(os.getenv("RES_LOOKBACK", "50"))          # direnc kac mumdan hesaplansin
RES_BREAK_PCT = float(os.getenv("RES_BREAK_PCT", "0.5"))     # direncten en az % kac yukarida kapanmali
MIN_CANDLE_BODY_PCT = float(os.getenv("MIN_CANDLE_BODY_PCT", "10.0"))  # kirilim mumunun govdesi min %

# --- LIKIDITE FILTRESI ---
USE_LIQUIDITY_FILTER = os.getenv("USE_LIQUIDITY_FILTER", "true").lower() == "true"
MIN_QUOTE_VOLUME_24H = float(os.getenv("MIN_QUOTE_VOLUME_24H", "5000000"))  # 5 milyon USDT

# --- YENI: PARA TAKIBI (ORDER FLOW) FILTRESI ---
USE_MONEY_FLOW_FILTER = os.getenv("USE_MONEY_FLOW_FILTER", "true").lower() == "true"
MIN_TAKER_BUY_RATIO = float(os.getenv("MIN_TAKER_BUY_RATIO", "0.55"))          # kirilim mumunda min taker-buy orani
AVG_TAKER_LOOKBACK = int(os.getenv("AVG_TAKER_LOOKBACK", "10"))                # kirilim ONCESI kac mumun ortalamasi
MIN_AVG_TAKER_BUY_RATIO = float(os.getenv("MIN_AVG_TAKER_BUY_RATIO", "0.50"))  # o ortalamanin min degeri

# --- YENI (opsiyonel, varsayilan kapali): MFI FILTRESI ---
USE_MFI_FILTER = os.getenv("USE_MFI_FILTER", "false").lower() == "true"
MFI_PERIOD = int(os.getenv("MFI_PERIOD", "14"))
MFI_THRESHOLD = float(os.getenv("MFI_THRESHOLD", "50.0"))   # MFI bu degerin ustunde olmali (para girisi baskin)

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
        # tbv = taker buy base asset volume -> agresif ALICI tarafindan gerceklesen hacim (PARA TAKIBI icin kritik)
        for c in ["open", "high", "low", "close", "volume", "tbv"]:
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
    TUM sembollerin 24s USDT hacmini TEK istekte ceker (parametresiz cagrilirsa
    Binance hepsini doner). Boylece her coin icin ayri istek atmaya gerek kalmaz,
    tarama suresine ek yuk binmez.
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
# PARA TAKIBI (ORDER FLOW / MONEY FLOW) HESAPLARI
# ══════════════════════════════════════════════════════════════════
def taker_buy_ratio(row):
    """Bir mumun hacminin ne kadari agresif ALICIDAN geldi (0-1 arasi)."""
    vol = float(row["volume"])
    if vol <= 0:
        return 0.5
    return float(row["tbv"]) / vol


def calc_mfi(df, period=14):
    """
    Klasik Money Flow Index. Fiyat + hacmi birlikte kullanir; 0-100 arasi doner.
    50 uzeri = para girisi baskin, 50 alti = para cikisi baskin.
    df: high/low/close/volume kolonlari olan, ESKIDEN YENIYE siralanmis DataFrame.
    """
    typical_price = (df["high"] + df["low"] + df["close"]) / 3.0
    raw_money_flow = typical_price * df["volume"]

    tp_diff = typical_price.diff()
    positive_flow = raw_money_flow.where(tp_diff > 0, 0.0)
    negative_flow = raw_money_flow.where(tp_diff < 0, 0.0)

    pos_sum = positive_flow.rolling(period).sum()
    neg_sum = negative_flow.rolling(period).sum()

    money_ratio = pos_sum / neg_sum.replace(0, np.nan)
    mfi = 100 - (100 / (1 + money_ratio))
    return mfi


# ══════════════════════════════════════════════════════════════════
# SINYAL: DIRENC KIRILIMI + PARA TAKIBI ONAYI
# ══════════════════════════════════════════════════════════════════
@dataclass
class Signal:
    symbol: str
    price: float
    resistance: float
    break_pct: float
    body_pct: float
    bar_time: str
    taker_buy_ratio_breakout: float
    avg_taker_buy_ratio_pre: float
    mfi_value: float = None


def analyze_symbol(symbol, quote_volumes=None):
    # --- likidite kontrolu once yapilir, dusukse hic kline cekmeye gerek yok ---
    if USE_LIQUIDITY_FILTER:
        qv = (quote_volumes or {}).get(symbol, 0.0)
        if qv < MIN_QUOTE_VOLUME_24H:
            return None

    # RES_LOOKBACK (direnc icin gecmis mum) + AVG_TAKER_LOOKBACK icin de yeterli pay + kirilim mumu
    needed = max(RES_LOOKBACK, MFI_PERIOD if USE_MFI_FILTER else 0) + AVG_TAKER_LOOKBACK + 5
    df = get_klines_closed(symbol, TIMEFRAME, limit=needed)
    if df is None or len(df) < RES_LOOKBACK + AVG_TAKER_LOOKBACK + 1:
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

    # --- YENI: PARA TAKIBI ONAYI ---
    tbr_breakout = taker_buy_ratio(breakout)
    pre_window = df.iloc[-(AVG_TAKER_LOOKBACK + 1):-1]   # kirilim mumu HARIC, hemen oncesindeki mumlar
    avg_tbr_pre = float(pre_window.apply(taker_buy_ratio, axis=1).mean())

    mfi_value = None
    if USE_MFI_FILTER:
        mfi_series = calc_mfi(df, period=MFI_PERIOD)
        mfi_value = float(mfi_series.iloc[-1]) if not pd.isna(mfi_series.iloc[-1]) else None

    if USE_MONEY_FLOW_FILTER:
        if tbr_breakout < MIN_TAKER_BUY_RATIO:
            return None  # kirilim mumunda alici baskinligi yeterli degil
        if avg_tbr_pre < MIN_AVG_TAKER_BUY_RATIO:
            return None  # kirilim ONCESINDE surdurulebilir para girisi yok -> muhtemelen ani/rastgele spike

    if USE_MFI_FILTER:
        if mfi_value is None or mfi_value < MFI_THRESHOLD:
            return None

    return Signal(
        symbol=symbol, price=close_now, resistance=resistance,
        break_pct=break_pct, body_pct=body_pct, bar_time=bar_time,
        taker_buy_ratio_breakout=tbr_breakout, avg_taker_buy_ratio_pre=avg_tbr_pre,
        mfi_value=mfi_value,
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
        "🟢 <b>DİRENÇ KIRILIMI + PARA GİRİŞİ</b>",
        sep,
        f"💱 <b>Coin:</b> {coin}",
        f"💰 <b>Fiyat:</b> {signal.price:.6f}",
        f"📍 <b>Kırılan Direnç ({TIMEFRAME}, son {RES_LOOKBACK} mum):</b> {signal.resistance:.6f}",
        f"📏 <b>Kırılım Mesafesi:</b> %{signal.break_pct:.2f}",
        f"🕯️ <b>Mum Gövdesi:</b> %{signal.body_pct:.2f}",
        f"💸 <b>Kırılım Mumu Alıcı Oranı:</b> %{signal.taker_buy_ratio_breakout*100:.1f}",
        f"📈 <b>Kırılım Öncesi Ort. Alıcı Oranı ({AVG_TAKER_LOOKBACK} mum):</b> %{signal.avg_taker_buy_ratio_pre*100:.1f}",
    ]
    if signal.mfi_value is not None:
        lines.append(f"🧭 <b>MFI:</b> {signal.mfi_value:.1f}")
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
    log.info(f"TARAMA BASLADI | Coin: {total} | Workers: {MAX_WORKERS} | TF: {TIMEFRAME} | Direnc lookback: {RES_LOOKBACK}")

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
    log.info("15DK DIRENC KIRILIM SCANNER baslatildi")
    log.info(f"Max coin       : {MAX_COINS}")
    log.info(f"Workers        : {MAX_WORKERS}")
    log.info(f"TF             : {TIMEFRAME}")
    log.info(f"Direnc lookback: {RES_LOOKBACK} mum")
    log.info(f"Min kirilim    : %{RES_BREAK_PCT}")
    log.info(f"Min mum govdesi: %{MIN_CANDLE_BODY_PCT}")
    log.info(f"Likidite filtre: {USE_LIQUIDITY_FILTER} (min {MIN_QUOTE_VOLUME_24H/1e6:.1f}M USDT)")
    log.info(f"Para takibi filtre: {USE_MONEY_FLOW_FILTER} (kirilim min %{MIN_TAKER_BUY_RATIO*100:.0f} alici, "
              f"onceki {AVG_TAKER_LOOKBACK} mum ort. min %{MIN_AVG_TAKER_BUY_RATIO*100:.0f} alici)")
    log.info(f"MFI filtre     : {USE_MFI_FILTER} (period={MFI_PERIOD}, esik={MFI_THRESHOLD})")
    log.info(f"Tarama araligi : {SCAN_INTERVAL} sn")
    log.info(f"Cooldown       : {SIGNAL_COOLDOWN} sn")
    log.info("=" * 60)

    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log.error("TELEGRAM_TOKEN ve TELEGRAM_CHAT_ID eksik!")
        return

    send_telegram(
        "🚀 DIRENC KIRILIM SCANNER BASLADI\n"
        + "=" * 30 + "\n"
        f"💱 TF: {TIMEFRAME}\n"
        f"📍 Direnc: son {RES_LOOKBACK} mumun en yuksegi\n"
        f"📏 Min kirilim: %{RES_BREAK_PCT}\n"
        f"🕯️ Min mum govdesi: %{MIN_CANDLE_BODY_PCT}\n"
        f"💧 Min likidite: {MIN_QUOTE_VOLUME_24H/1e6:.1f}M USDT\n"
        f"💸 Para takibi: {USE_MONEY_FLOW_FILTER} (min %{MIN_TAKER_BUY_RATIO*100:.0f} alici / kirilim, "
        f"onceki {AVG_TAKER_LOOKBACK} mum ort. min %{MIN_AVG_TAKER_BUY_RATIO*100:.0f})\n"
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
