"""
Kirilim + Volume Delta Teyit Scanner - Binance Futures
--------------------------------------------------------
Mantik:
1) KIRILIM: Fiyat, onceki 20 mumun en yuksek/en dusuk seviyesini
   %0.5 tampon ile kirar (MONEY TRADER - FIBO TRADE pine kodundaki
   ayni mantik)
2) HACIM ONAYI: O mumun hacmi, 20 periyotluk ortalama hacmin 2 katindan
   buyuk (miktar teyidi - yon bilgisi yok)
3) MOMENTUM ONAYI: RSI(14) ve MACD(12,26,9) kirilim yonunu destekliyor
4) DELTA TEYIDI (yeni eklenen katman): Ayni mumda agresif alici/satici
   hacmi (delta), son 20 mumun ortalama mutlak deltasinin 5 katindan
   fazla VE kirilim yonuyle ayni yonde
   -> Bu, "hacim buyuk ama kimin yonunde" sorusunu cevaplar. Kirilim
      + hacim + RSI/MACD hepsi tutsa bile delta karisiksa (yon net
      degilse) sinyal ELENIR - cunku bu genelde sahte kirilim/spike-
      and-reverse paterni demek.

Sadece TUM katmanlar ayni anda tutarsa sinyal uretilir (AND mantigi).
Render.com'da background worker olarak calisir, her 5 dakikada bir tarar.
"""

import time
import requests
from datetime import datetime, timezone

# ============ AYARLAR ============
BINANCE_FUTURES_BASE = "https://fapi.binance.com"
KLINE_INTERVAL = "5m"
FETCH_LIMIT = 100           # RSI/MACD/EMA'nin saglikli hesaplanmasi icin yeterli gecmis

# Kirilim ayarlari (pine kodundaki bo_len, bo_buffer_pct, vol_mult ile ayni)
BREAKOUT_LOOKBACK = 20
BREAKOUT_BUFFER_PCT = 0.5
VOLUME_MULTIPLIER = 2.0

# Momentum ayarlari (pine kodundaki rsi_bull/rsi_bear ile ayni)
RSI_LENGTH = 14
RSI_BULL_MIN = 60
RSI_BEAR_MAX = 40
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9

# Delta teyit ayarlari
DELTA_LOOKBACK = 20
DELTA_SPIKE_MULTIPLIER = 5.0

MIN_24H_VOLUME_USDT = 3_000_000
COOLDOWN_HOURS = 4
SCAN_INTERVAL_SECONDS = 300  # 5 dakikada bir tara

TELEGRAM_BOT_TOKEN = "BURAYA_TELEGRAM_BOT_TOKEN"
TELEGRAM_CHAT_ID = "BURAYA_CHAT_ID"

last_signal_time = {}


# ============ TELEGRAM ============
def send_telegram_message(text: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    try:
        r = requests.post(url, data=payload, timeout=10)
        if r.status_code != 200:
            print(f"[Telegram Hatasi] {r.status_code} - {r.text}")
    except Exception as e:
        print(f"[Telegram Gonderim Hatasi] {e}")


# ============ BINANCE VERI CEKME ============
def get_usdt_perpetual_symbols():
    """3M USDT ustu 24h hacme sahip USDT-M perpetual coinleri getirir."""
    url = f"{BINANCE_FUTURES_BASE}/fapi/v1/ticker/24hr"
    resp = requests.get(url, timeout=15)
    resp.raise_for_status()
    data = resp.json()

    symbols = []
    for item in data:
        symbol = item["symbol"]
        if not symbol.endswith("USDT"):
            continue
        quote_volume = float(item.get("quoteVolume", 0))
        if quote_volume >= MIN_24H_VOLUME_USDT:
            symbols.append(symbol)
    return symbols


def get_klines(symbol: str, limit: int = FETCH_LIMIT):
    url = f"{BINANCE_FUTURES_BASE}/fapi/v1/klines"
    params = {"symbol": symbol, "interval": KLINE_INTERVAL, "limit": limit}
    resp = requests.get(url, params=params, timeout=15)
    resp.raise_for_status()
    return resp.json()


# ============ GOSTERGE HESAPLAMALARI ============
def calculate_ema(values, length):
    """Basit EMA (ilk deger seed olarak alinir)."""
    alpha = 2 / (length + 1)
    ema_values = [values[0]]
    for v in values[1:]:
        ema_values.append(alpha * v + (1 - alpha) * ema_values[-1])
    return ema_values


def calculate_rsi(closes, length=RSI_LENGTH):
    """Wilder's smoothing ile RSI - son degeri dondurur."""
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [d if d > 0 else 0.0 for d in deltas]
    losses = [-d if d < 0 else 0.0 for d in deltas]

    avg_gain = sum(gains[:length]) / length
    avg_loss = sum(losses[:length]) / length

    for i in range(length, len(gains)):
        avg_gain = (avg_gain * (length - 1) + gains[i]) / length
        avg_loss = (avg_loss * (length - 1) + losses[i]) / length

    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def calculate_macd(closes, fast=MACD_FAST, slow=MACD_SLOW, signal=MACD_SIGNAL):
    """Son MACD line, signal line ve histogram degerlerini dondurur."""
    ema_fast = calculate_ema(closes, fast)
    ema_slow = calculate_ema(closes, slow)
    macd_line = [f - s for f, s in zip(ema_fast, ema_slow)]
    signal_line = calculate_ema(macd_line, signal)
    hist = macd_line[-1] - signal_line[-1]
    return macd_line[-1], signal_line[-1], hist


def calculate_delta(kline):
    """
    Binance kline index sirasi:
    5 = volume, 9 = taker_buy_base_asset_volume
    delta = (2 * taker_buy_volume) - total_volume
    """
    total_volume = float(kline[5])
    taker_buy_volume = float(kline[9])
    if total_volume == 0:
        return 0.0
    return (2 * taker_buy_volume) - total_volume


# ============ COOLDOWN ============
def is_on_cooldown(symbol: str) -> bool:
    last_time = last_signal_time.get(symbol)
    if last_time is None:
        return False
    elapsed_hours = (time.time() - last_time) / 3600
    return elapsed_hours < COOLDOWN_HOURS


# ============ ANA SINYAL MANTIGI ============
def analyze_symbol(symbol: str):
    try:
        klines = get_klines(symbol, FETCH_LIMIT)
    except Exception as e:
        print(f"[{symbol}] Kline cekme hatasi: {e}")
        return

    min_required = max(MACD_SLOW + MACD_SIGNAL, BREAKOUT_LOOKBACK + 1, DELTA_LOOKBACK + 1) + 5
    if len(klines) < min_required:
        return

    closes = [float(k[4]) for k in klines]
    highs = [float(k[2]) for k in klines]
    lows = [float(k[3]) for k in klines]
    volumes = [float(k[5]) for k in klines]

    current_kline = klines[-1]
    current_close = closes[-1]
    prev_close = closes[-2]
    current_volume = volumes[-1]

    # ---- 1) KIRILIM SEVIYELERI (mevcut mum haric, onceki BREAKOUT_LOOKBACK mum) ----
    resistance_level = max(highs[-(BREAKOUT_LOOKBACK + 1):-1])
    support_level = min(lows[-(BREAKOUT_LOOKBACK + 1):-1])

    breakout_up = (
        current_close > resistance_level * (1 + BREAKOUT_BUFFER_PCT / 100)
        and prev_close <= resistance_level
    )
    breakout_down = (
        current_close < support_level * (1 - BREAKOUT_BUFFER_PCT / 100)
        and prev_close >= support_level
    )

    if not (breakout_up or breakout_down):
        return  # kirilim yoksa devam etmeye gerek yok

    # ---- 2) HACIM ONAYI (miktar) ----
    vol_sma20 = sum(volumes[-21:-1]) / 20
    volume_confirmed = current_volume > vol_sma20 * VOLUME_MULTIPLIER

    # ---- 3) MOMENTUM ONAYI ----
    rsi_value = calculate_rsi(closes, RSI_LENGTH)
    macd_line, signal_line, hist = calculate_macd(closes)

    mom_bullish = rsi_value > RSI_BULL_MIN and hist > 0 and macd_line > signal_line
    mom_bearish = rsi_value < RSI_BEAR_MAX and hist < 0 and macd_line < signal_line

    # ---- 4) DELTA TEYIDI (yon) ----
    previous_deltas = [abs(calculate_delta(k)) for k in klines[-(DELTA_LOOKBACK + 1):-1]]
    avg_abs_delta = sum(previous_deltas) / len(previous_deltas)
    current_delta = calculate_delta(current_kline)

    delta_ratio = abs(current_delta) / avg_abs_delta if avg_abs_delta > 0 else 0
    delta_spike = delta_ratio >= DELTA_SPIKE_MULTIPLIER

    # ---- NIHAI SINYAL: hepsi ayni anda tutmali ----
    final_buy = (
        breakout_up and volume_confirmed and mom_bullish
        and delta_spike and current_delta > 0
    )
    final_sell = (
        breakout_down and volume_confirmed and mom_bearish
        and delta_spike and current_delta < 0
    )

    if not (final_buy or final_sell):
        return

    if is_on_cooldown(symbol):
        return

    direction = "KIRILIM AL (Yukari)" if final_buy else "KIRILIM SAT (Asagi)"
    emoji = "🟢" if final_buy else "🔴"

    message = (
        f"{emoji} <b>Kirilim + Delta Teyitli Sinyal</b>\n\n"
        f"<b>Coin:</b> {symbol}\n"
        f"<b>Yon:</b> {direction}\n"
        f"<b>Fiyat:</b> {current_close}\n"
        f"<b>RSI:</b> {rsi_value:.1f}\n"
        f"<b>MACD Hist:</b> {hist:.6f}\n"
        f"<b>Hacim / Ortalama:</b> {current_volume / vol_sma20:.2f}x\n"
        f"<b>Delta / Ortalama:</b> {delta_ratio:.1f}x\n"
        f"<b>Zaman Dilimi:</b> {KLINE_INTERVAL}\n"
        f"<b>Saat (UTC):</b> {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')}"
    )

    print(message.replace("<b>", "").replace("</b>", ""))
    send_telegram_message(message)
    last_signal_time[symbol] = time.time()


# ============ TARAMA DONGUSU ============
def run_scan_cycle():
    print(f"\n=== Tarama basladi: {datetime.now(timezone.utc)} ===")
    try:
        symbols = get_usdt_perpetual_symbols()
    except Exception as e:
        print(f"Sembol listesi cekme hatasi: {e}")
        return

    print(f"{len(symbols)} coin taraniyor (3M+ USDT 24h hacim filtresi)")

    for symbol in symbols:
        analyze_symbol(symbol)
        time.sleep(0.15)  # Binance rate limit icin kucuk bekleme

    print("=== Tarama tamamlandi ===")


def main():
    send_telegram_message("✅ Kirilim + Volume Delta Teyit Scanner baslatildi.")
    while True:
        run_scan_cycle()
        time.sleep(SCAN_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
