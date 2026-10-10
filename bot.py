import os
import time
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
import pandas as pd
import requests

# ==============================================================================
# YAPILANDIRMA (Detaylı Log Modu)
# ==============================================================================
TIMEFRAME = os.getenv("TIMEFRAME", "1m")                    
MIN_CANDLE_PCT = float(os.getenv("MIN_CANDLE_PCT", 0.8))     
VOLUME_MULTIPLIER = float(os.getenv("VOLUME_MULTIPLIER", 1.5)) 
SCAN_INTERVAL = int(os.getenv("SCAN_INTERVAL", 60))          
MAX_WORKERS = int(os.getenv("MAX_WORKERS", 10))

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


def calculate_delta(df):
    df = df.copy()
    # Binance Taker Buy/Sell tabanlı delta
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

        df_ind = calculate_delta(closed_df)

        m1 = df_ind.iloc[-2]  # 1. Mum (Kırılım mumu)
        m2 = df_ind.iloc[-1]  # 2. Mum (Teyit mumu)
        
        avg_volume = df_ind['volume'].iloc[-25:-2].mean()

        m1_body_pct = (abs(m1['close'] - m1['open']) / m1['open']) * 100.0
        is_volume_spike = m1['volume'] >= (avg_volume * VOLUME_MULTIPLIER)

        # Eğer gövde veya hacim şartı tutmuyorsa sessizce geç
        if not is_volume_spike or (m1_body_pct < MIN_CANDLE_PCT):
            return None

        # --- YÖN KONTROLÜ ---
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
            # Şartları sağlayan bir mum bulduk ama 2. mum teyidi veya delta tutmadı!
            # Bunu konsola yazdıralım ki nerede takıldığını görelim.
            print(f"[Filtreye Takıldı] {symbol} -> Hacim/Gövde geçti ancak 2. Mum / Delta teyidi uymadı.")
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

    except Exception as e:
        return None


def run_scanner():
    now = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"\n[{now} UTC] 🔍 Tarama Başlatıldı ({TIMEFRAME})...")

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
        badge = "🟢 SİNYAL (LONG)" if is_long else "🔴 SİNYAL (SHORT)"
        
        log_msg = (
            f"========================================\n"
            f"{badge}\n"
            f"Sembol: {s['symbol'].replace('USDT', '')}/USDT\n"
            f"Fiyat: ${s['price']}\n"
            f"1. Mum Gövdesi: %{s['m1_body']:.2f}\n"
            f"Hacim Çarpanı: {s['m1_vol_multi']:.1f}x\n"
            f"========================================"
        )
        print(log_msg)
        send_telegram_alert(log_msg)


def main():
    print("🚀 HATA AYIKLAMA MODU AKTİF")
    while True:
        try:
            run_scanner()
        except Exception as e:
            print(f"[Sistem Hatası] Tarama döngüsü hatası: {e}")
        time.sleep(SCAN_INTERVAL)


if __name__ == "__main__":
    main()
