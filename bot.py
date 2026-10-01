import os
import time
import requests
from datetime import datetime, timezone

# ============ AYARLAR (ENV VARIABLES) ============
BINANCE_FUTURES_BASE = "https://fapi.binance.com"

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# Strateji Parametreleri
TRIGGER_TIMEFRAME = os.environ.get("TRIGGER_TIMEFRAME", "15m")
TRIGGER_CHANGE_PCT = float(os.environ.get("TRIGGER_CHANGE_PCT", "10.0"))
WATCHLIST_BAR_LIMIT = int(os.environ.get("WATCHLIST_BAR_LIMIT", "100"))

BREAKOUT_TIMEFRAME = os.environ.get("BREAKOUT_TIMEFRAME", "1m")
BREAKOUT_LOOKBACK = int(os.environ.get("BREAKOUT_LOOKBACK", "20"))
BREAKOUT_BUFFER_PCT = float(os.environ.get("BREAKOUT_BUFFER_PCT", "0.5"))
VOLUME_MULTIPLIER = float(os.environ.get("VOLUME_MULTIPLIER", "2.0"))

RSI_LENGTH = int(os.environ.get("RSI_LENGTH", "14"))
RSI_BULL_MIN = float(os.environ.get("RSI_BULL_MIN", "60"))
RSI_BEAR_MAX = float(os.environ.get("RSI_BEAR_MAX", "40"))
MACD_FAST = int(os.environ.get("MACD_FAST", "12"))
MACD_SLOW = int(os.environ.get("MACD_SLOW", "26"))
MACD_SIGNAL = int(os.environ.get("MACD_SIGNAL", "9"))

DELTA_LOOKBACK = int(os.environ.get("DELTA_LOOKBACK", "20"))
DELTA_SPIKE_MULTIPLIER = float(os.environ.get("DELTA_SPIKE_MULTIPLIER", "5.0"))

MIN_24H_VOLUME_USDT = float(os.environ.get("MIN_24H_VOLUME_USDT", "3000000"))
MIN_CANDLE_VOLUME_USDT = float(os.environ.get("MIN_CANDLE_VOLUME_USDT", "1000000"))
COOLDOWN_HOURS = float(os.environ.get("COOLDOWN_HOURS", "4"))
SCAN_INTERVAL_SECONDS = int(os.environ.get("SCAN_INTERVAL_SECONDS", "60"))

# State Management (Bellek/Hafıza)
watchlist = {}          # {"COIN": {"remaining_bars": 100, "trigger_dir": "UP/DOWN", "trigger_time": ts}}
last_signal_time = {}   # Cooldown takibi


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


# ============ BINANCE API ============
def get_usdt_perpetual_symbols():
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


def get_klines(symbol: str, interval: str, limit: int = 100):
    url = f"{BINANCE_FUTURES_BASE}/fapi/v1/klines"
    params = {"symbol": symbol, "interval": interval, "limit": limit}
    resp = requests.get(url, params=params, timeout=15)
    resp.raise_for_status()
    return resp.json()


# ============ İNDİKATÖR HESAPLAMALARI ============
def calculate_ema(values, length):
    alpha = 2 / (length + 1)
    ema_values = [values[0]]
    for v in values[1:]:
        ema_values.append(alpha * v + (1 - alpha) * ema_values[-1])
    return ema_values


def calculate_rsi(closes, length=RSI_LENGTH):
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
    ema_fast = calculate_ema(closes, fast)
    ema_slow = calculate_ema(closes, slow)
    macd_line = [f - s for f, s in zip(ema_fast, ema_slow)]
    signal_line = calculate_ema(macd_line, signal)
    hist = macd_line[-1] - signal_line[-1]
    return macd_line[-1], signal_line[-1], hist


def calculate_delta(kline):
    total_volume = float(kline[5])
    taker_buy_volume = float(kline[9])
    if total_volume == 0:
        return 0.0
    return (2 * taker_buy_volume) - total_volume


def is_on_cooldown(symbol: str) -> bool:
    last_time = last_signal_time.get(symbol)
    if last_time is None:
        return False
    elapsed_hours = (time.time() - last_time) / 3600
    return elapsed_hours < COOLDOWN_HOURS


# ============ AŞAMA 1: 15M TETİKLENME KONTROLÜ ============
def check_15m_trigger(symbol: str):
    if symbol in watchlist:
        return

    try:
        klines = get_klines(symbol, TRIGGER_TIMEFRAME, limit=3)
    except Exception:
        return

    if len(klines) < 2:
        return

    last_closed = klines[-2]
    open_price = float(last_closed[1])
    close_price = float(last_closed[4])

    if open_price == 0:
        return

    change_pct = ((close_price - open_price) / open_price) * 100

    if abs(change_pct) >= TRIGGER_CHANGE_PCT:
        direction = "LONG" if change_pct > 0 else "SHORT"
        watchlist[symbol] = {
            "remaining_bars": WATCHLIST_BAR_LIMIT,
            "trigger_dir": direction,
            "trigger_time": datetime.now(timezone.utc).strftime('%H:%M:%S')
        }
        print(f" 🔥 [TETİKLENDİ] {symbol} (15m %{change_pct:.1f}) -> Takip Listesine Alındı!")


# ============ AŞAMA 2: 1M KIRILIM ANALİZİ ============
def analyze_1m_breakout(symbol: str):
    try:
        klines = get_klines(symbol, BREAKOUT_TIMEFRAME, limit=100)
    except Exception:
        return

    min_req = max(MACD_SLOW + MACD_SIGNAL, BREAKOUT_LOOKBACK + 1, DELTA_LOOKBACK + 1) + 5
    if len(klines) < min_req:
        return

    closes = [float(k[4]) for k in klines]
    highs = [float(k[2]) for k in klines]
    lows = [float(k[3]) for k in klines]
    volumes = [float(k[5]) for k in klines]

    current_kline = klines[-1]
    current_close = closes[-1]
    prev_close = closes[-2]
    current_volume = volumes[-1]
    current_quote_volume = float(current_kline[7])

    if current_quote_volume < MIN_CANDLE_VOLUME_USDT:
        return

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
        return

    vol_sma20 = sum(volumes[-21:-1]) / 20
    volume_confirmed = current_volume > vol_sma20 * VOLUME_MULTIPLIER

    rsi_value = calculate_rsi(closes, RSI_LENGTH)
    macd_line, signal_line, hist = calculate_macd(closes)

    mom_bullish = rsi_value > RSI_BULL_MIN and hist > 0 and macd_line > signal_line
    mom_bearish = rsi_value < RSI_BEAR_MAX and hist < 0 and macd_line < signal_line

    previous_deltas = [abs(calculate_delta(k)) for k in klines[-(DELTA_LOOKBACK + 1):-1]]
    avg_abs_delta = sum(previous_deltas) / len(previous_deltas) if len(previous_deltas) > 0 else 1
    current_delta = calculate_delta(current_kline)

    delta_ratio = abs(current_delta) / avg_abs_delta if avg_abs_delta > 0 else 0
    delta_spike = delta_ratio >= DELTA_SPIKE_MULTIPLIER

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

    direction = "KIRILIM AL (Yukarı)" if final_buy else "KIRILIM SAT (Aşağı)"
    emoji = "🟢" if final_buy else "🔴"

    message = (
        f"{emoji} <b>1m Kırılım + Delta Teyitli Sinyal</b>\n\n"
        f"<b>Coin:</b> {symbol}\n"
        f"<b>Yön:</b> {direction}\n"
        f"<b>Fiyat:</b> {current_close}\n"
        f"<b>Mum Hacmi:</b> {current_quote_volume:,.0f} USDT\n"
        f"<b>RSI:</b> {rsi_value:.1f}\n"
        f"<b>Hacim / Ort:</b> {current_volume / vol_sma20:.2f}x\n"
        f"<b>Delta / Ort:</b> {delta_ratio:.1f}x\n"
        f"<b>Tetiklenme Zamanı (15m):</b> {watchlist[symbol]['trigger_time']} UTC\n"
        f"<b>Saat (UTC):</b> {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')}"
    )

    print(f"🚀 [SİNYAL BİLDİRİMİ SÜRÜLDÜ] {symbol} - {direction}")
    send_telegram_message(message)

    last_signal_time[symbol] = time.time()
    if symbol in watchlist:
        del watchlist[symbol]


# ============ ANA DÖNGÜ ============
def run_scan_cycle():
    now_str = datetime.now(timezone.utc).strftime('%H:%M:%S')
    
    try:
        symbols = get_usdt_perpetual_symbols()
    except Exception as e:
        print(f"[{now_str}] ❌ Sembol listesi çekme hatası: {e}")
        return

    print(f"[{now_str}] 🔍 {len(symbols)} coin taranıyor (15m %{TRIGGER_CHANGE_PCT} hareketi aranıyor)...")

    # 1. AŞAMA: 15m %10 Hareket Yapanları Bul
    for symbol in symbols:
        check_15m_trigger(symbol)
        time.sleep(0.03)

    # 2. AŞAMA: Takip Listesindeki Coin'lerin 1m Kırılımını Tara
    active_coins = list(watchlist.keys())
    
    if len(active_coins) > 0:
        print(f"[{now_str}] 📋 Takip Listesi ({len(active_coins)} Coin): {', '.join([f'{c}({watchlist[c][\"remaining_bars\"]}b)' for c in active_coins])}")
    else:
        print(f"[{now_str}] 💤 Takip listesinde coin yok.")

    for symbol in active_coins:
        analyze_1m_breakout(symbol)
        
        # Sayaç Düşürme Yönetimi
        if symbol in watchlist:
            watchlist[symbol]["remaining_bars"] -= 1
            if watchlist[symbol]["remaining_bars"] <= 0:
                print(f"⌛ [SÜRE DOLDU] {symbol} 100 bar boyunca kırılım yapmadı. Takipten çıkarıldı.")
                del watchlist[symbol]
        time.sleep(0.05)


def main():
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("[HATA] TELEGRAM_TOKEN veya TELEGRAM_CHAT_ID eksik!")
        return

    print("🤖 Bot Başlatıldı...")
    send_telegram_message("✅ <b>2 Aşamalı (15m Trigger + 1m Breakout) Scanner Başlatıldı.</b>")

    while True:
        run_scan_cycle()
        time.sleep(SCAN_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
