import os
import time
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
import pandas as pd
import requests

# ==============================================================================
# YAPILANDIRMA VE EŞİK DEĞERLERİ (ATR ve SMA Kaldırıldı)
# ==============================================================================
TIMEFRAME = os.getenv("TIMEFRAME", "5m")                    
MIN_CANDLE_PCT = float(os.getenv("MIN_CANDLE_PCT", 2.0))     # Mum Gövde Şartı
VOLUME_MULTIPLIER = float(os.getenv("VOLUME_MULTIPLIER", 3.0)) # 3 Katı Hacim Sıçraması
SCAN_INTERVAL = int(os.getenv("SCAN_INTERVAL", 180))         # 180 Saniye (3 Dakika)
MAX_WORKERS = int(os.getenv("MAX_WORKERS", 10))
DEBUG_LOG = os.getenv("DEBUG_LOG", "false").lower() == "true"

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

API_BASE = "https://fapi.binance.com"


def send_telegram_alert(message: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}
    try:
        requests.post(url, json=payload, timeout=5)
    except Exception as e:
        print(f"[Hata] Telegram bildirimi gönderilemedi: {e}")


def calculate_strategy_indicators(df):
    df = df.copy()
    # Delta Hesabı (Taker Buy Base - Taker Sell Base)
    df['taker_sell_base'] = df['volume'] - df['taker_buy_base']
    df['delta'] = df['taker_buy_base'] - df['taker_sell_base']
    return df


def get_usdt_symbols():
    try:
        response = requests.get(f"{API_BASE}/fapi/v1/ticker/24hr", timeout=10)
        tickers = response.json()
        return [t['symbol'] for t in tickers if t['symbol'].endswith('USDT')]
    except Exception as e:
        print(f"[Hata] Ticker verisi alınamadı: {e}")
        return []


def analyze_symbol(symbol: str):
    try:
        url = f"{API_BASE}/fapi/v1/klines"
        params = {
            "symbol": symbol,
            "interval": TIMEFRAME,
            "limit": 40
        }
        res = requests.get(url, params=params, timeout=5)
        klines = res.json()

        if not klines or len(klines) < 30:
            return None

        df = pd.DataFrame(klines, columns=[
            'open_time', 'open', 'high', 'low', 'close', 'volume',
            'close_time', 'quote_volume', 'trades', 'taker_buy_base',
            'taker_buy_quote', 'ignore'
        ])

        # Canlı mumu çıkar, kapanmış son mumlar üzerinden net teyit al
        closed_df = df.iloc[:-1].copy()
        
        for col in ['open', 'high', 'low', 'close', 'volume', 'taker_buy_base']:
            closed_df[col] = closed_df[col].astype(float)

        df_ind = calculate_strategy_indicators(closed_df)

        m1 = df_ind.iloc[-2]  # 1. Mum (Kırılım / Hareket Mumu)
        m2 = df_ind.iloc[-1]  # 2. Mum (Teyit Mumu)
        
        avg_volume = df_ind['volume'].iloc[-25:-2].mean()

        m1_body_pct = (abs(m1['close'] - m1['open']) / m1['open']) * 100.0
        is_volume_spike = m1['volume'] >= (avg_volume * VOLUME_MULTIPLIER)

        if not is_volume_spike or (m1_body_pct < MIN_CANDLE_PCT):
            return None

        # --- YÖN KONTROLÜ (LONG vs SHORT - SMA Olmadan Saf Kırılım ve Delta) ---
        
        # LONG ŞARTLARI: Yeşil gövde, pozitif 3x hacim deltası, 2. mumda yukarı kapanış ve kesintisiz pozitif delta
        m1_green = m1['close'] > m1['open']
        is_positive_delta_1 = m1['delta'] > 0
        m2_closes_higher = m2['close'] > m1['close']
        is_positive_delta_2 = m2['delta'] > 0

        is_valid_long = (
            m1_green and 
            is_positive_delta_1 and 
            m2_closes_higher and 
            is_positive_delta_2
        )

        # SHORT ŞARTLARI: Kırmızı gövde, negatif 3x hacim deltası, 2. mumda aşağı kapanış ve kesintisiz negatif delta
        m1_red = m1['close'] < m1['open']
        is_negative_delta_1 = m1['delta'] < 0
        m2_closes_lower = m2['close'] < m1['close']
        is_negative_delta_2 = m2['delta'] < 0

        is_valid_short = (
            m1_red and 
            is_negative_delta_1 and 
            m2_closes_lower and 
            is_negative_delta_2
        )

        if not (is_valid_long or is_valid_short):
            return None

        direction = "LONG" if is_valid_long else "SHORT"

        return {
            "symbol": symbol,
            "direction": direction,
            "price": m2['close'],
            "m1_body": m1_body_pct,
            "m1_vol_multi": m1['volume'] / avg_volume,
            "m1_delta": m1['delta'],
            "m2_delta": m2['delta']
        }

    except Exception:
        return None


def run_scanner():
    now = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"\n[{now} UTC] 🔍 Saf Kırılım + Kesintisiz Delta Taraması Başlatıldı...")

    symbols = get_usdt_symbols()
    if not symbols:
        print("[!] Taranacak sembol bulunamadı.")
        return

    detected_signals = []

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        future_to_symbol = {executor.submit(analyze_symbol, sym): sym for sym in symbols}
        for future in as_completed(future_to_symbol):
            result = future.result()
            if result:
                detected_signals.append(result)

    print(f"[Tamamlandı] Taranan Çift: {len(symbols)} | Teyitli Sinyal: {len(detected_signals)}")

    for s in detected_signals:
        is_long = s['direction'] == 'LONG'
        badge = "🟢 SAF KIRILIM + DELTA SİNYALİ (LONG)" if is_long else "🔴 SAF KIRILIM + DELTA SİNYALİ (SHORT)"
        
        log_msg = (
            f"========================================\n"
            f"{badge}\n"
            f"Sembol: {s['symbol'].replace('USDT', '')}/USDT\n"
            f"Fiyat (2. Mum Kapanış): ${s['price']}\n"
            f"1. Mum Gövdesi: %{s['m1_body']:.2f}\n"
            f"Hacim Çarpanı: {s['m1_vol_multi']:.1f}x\n"
            f"1. Mum Delta: {s['m1_delta']:,.0f}\n"
            f"2. Mum Delta (Teyit): {s['m2_delta']:,.0f}\n"
            f"========================================"
        )
        print(log_msg)
        send_telegram_alert(log_msg)


def main():
    print("🚀 SAF DELTA BREAKOUT BOTU AKTİF (Filtresiz Mod)")
    print(f"Ayarlar -> Zaman Dilimi: {TIMEFRAME} | Tarama Aralığı: {SCAN_INTERVAL}s | Min Gövde: %{MIN_CANDLE_PCT} | Hacim Katı: {VOLUME_MULTIPLIER}x")
    while True:
        try:
            run_scanner()
        except Exception as e:
            print(f"[Sistem Hatası] Tarama döngüsü hatası: {e}")
        time.sleep(SCAN_INTERVAL)


if __name__ == "__main__":
    main()
