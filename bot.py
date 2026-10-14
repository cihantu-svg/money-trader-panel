import os
import time
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
import pandas as pd
import requests
from requests.adapters import HTTPAdapter

# ==============================================================================
# YAPILANDIRMA (Saf Kırılım + %5 Mum Boyu + 3dk Tarama)
# ==============================================================================
TIMEFRAME = os.getenv("TIMEFRAME", "15m")                    
SCAN_INTERVAL = int(os.getenv("SCAN_INTERVAL", 180))         # 3 dakika (180 saniye)
MAX_WORKERS = int(os.getenv("MAX_WORKERS", 8))               
DEBUG_LOG = os.getenv("DEBUG_LOG", "false").lower() == "true"

# Kırılım Parametreleri
BO_LEN = 20                 
BO_BUFFER_PCT = 0.5         

# GÜRÜLTÜ ENGELLEYİCİ: Kırılım mumu min %5 gövde büyüklüğünde olmalı
MIN_CANDLE_BODY_PCT = 5.0   

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

API_BASE = "https://fapi.binance.com"

session = requests.Session()
session.mount("https://", HTTPAdapter(pool_connections=MAX_WORKERS, pool_maxsize=MAX_WORKERS))

# Aynı mum için tekrar bildirim gitmesini önlemek için hafıza: {symbol: open_time}
last_alerted = {}

def send_telegram_alert(message: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("[Uyarı] TELEGRAM_TOKEN veya TELEGRAM_CHAT_ID ENV içinde bulunamadı!")
        return
        
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}
    try:
        r = session.post(url, json=payload, timeout=5)
        if r.status_code != 200:
            print(f"[Telegram Hata] Kod: {r.status_code} - Yanıt: {r.text}")
    except Exception as e:
        print(f"[Hata] Telegram bildirimi gönderilemedi: {e}")

def get_usdt_symbols():
    try:
        r = session.get(f"{API_BASE}/fapi/v1/exchangeInfo", timeout=15)
        if r.status_code != 200:
            return []
        data = r.json()
        return [
            s["symbol"]
            for s in data.get("symbols", [])
            if s.get("quoteAsset") == "USDT"
            and s.get("status") == "TRADING"
            and s.get("contractType") == "PERPETUAL"
        ]
    except Exception as e:
        print(f"[Hata] Sembol listesi alınamadı: {e}")
        return []

def calculate_levels(df):
    df = df.copy()
    # Kırılım Seviyeleri (Önceki 20 mumun en yüksek ve en düşük değerleri)
    df['resistance'] = df['high'].shift(1).rolling(window=BO_LEN).max()
    df['support'] = df['low'].shift(1).rolling(window=BO_LEN).min()
    return df

def analyze_symbol(symbol: str):
    try:
        res = session.get(
            f"{API_BASE}/fapi/v1/klines",
            params={"symbol": symbol, "interval": TIMEFRAME, "limit": 30},
            timeout=10,
        )
        if res.status_code in (418, 429):
            return None
        if res.status_code != 200:
            return None

        klines = res.json()
        if not isinstance(klines, list) or len(klines) < 25:
            return None

        df = pd.DataFrame(klines, columns=[
            'open_time', 'open', 'high', 'low', 'close', 'volume',
            'close_time', 'quote_volume', 'trades', 'taker_buy_base',
            'taker_buy_quote', 'ignore'
        ]).iloc[:, :11].astype(float)

        now_ms = int(time.time() * 1000)
        df = df[df["close_time"] < now_ms].reset_index(drop=True)

        if len(df) < BO_LEN + 2:
            return None

        df = calculate_levels(df)
        
        m1 = df.iloc[-2]  
        m = df.iloc[-1]   

        # --- 1. MUM BOYU / GÖVDE KONTROLÜ (Min %5 Şartı) ---
        candle_change_pct = ((m['close'] - m['open']) / m['open']) * 100.0
        abs_candle_change = abs(candle_change_pct)
        
        if abs_candle_change < MIN_CANDLE_BODY_PCT:
            return None

        # --- 2. SAF KIRILIM KOŞULLARI ---
        resistance_target = m['resistance'] * (1 + BO_BUFFER_PCT / 100)
        support_target = m['support'] * (1 - BO_BUFFER_PCT / 100)

        breakout_up = m['close'] > resistance_target and m1['close'] <= m['resistance']
        breakout_down = m['close'] < support_target and m1['close'] >= m['support']

        if not (breakout_up or breakout_down):
            return None

        direction = "LONG" if breakout_up else "SHORT"

        return {
            "symbol": symbol,
            "direction": direction,
            "price": m['close'],
            "candle_change": candle_change_pct,
            "open_time": int(m['open_time'])
        }

    except Exception:
        return None

def run_scanner():
    now = datetime.now(timezone.utc).strftime("%H:%M:%S")
    symbols = get_usdt_symbols()
    print(f"\n[{now} UTC] 🔍 Saf Kırılım Taraması Başlatıldı ({TIMEFRAME}) | Toplam Sembol: {len(symbols)}")
    if not symbols:
        return

    detected_signals = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        future_to_symbol = {executor.submit(analyze_symbol, sym): sym for sym in symbols}
        for future in as_completed(future_to_symbol):
            res = future.result()
            if res:
                detected_signals.append(res)

    print(f"[Tamamlandı] Teyitli Sinyal Sayısı: {len(detected_signals)}")

    for s in detected_signals:
        sym = s["symbol"]
        candle_time = s["open_time"]

        # Mükerrer Bildirim Kontrolü
        if last_alerted.get(sym) == candle_time:
            continue
        
        last_alerted[sym] = candle_time

        is_long = s['direction'] == 'LONG'
        badge = "🟢 15m SAF KIRILIM AL (Min %5 Mum)" if is_long else "🔴 15m SAF KIRILIM SAT (Min %5 Mum)"
        
        msg = (
            f"========================================\n"
            f"{badge}\n"
            f"Sembol: {sym.replace('USDT', '')}/USDT\n"
            f"Zaman Dilimi: {TIMEFRAME} (3dk Tarama)\n"
            f"Fiyat: ${s['price']}\n"
            f"Mum Değişimi: %{s['candle_change']:.2f}\n"
            f"========================================"
        )
        print(msg)
        send_telegram_alert(msg)

def main():
    print(f"🚀 SAF KIRILIM TARAYICI AKTİF ({TIMEFRAME} - Her 3dk bir - Min %5 Gövde)")
    while True:
        try:
            run_scanner()
        except Exception as e:
            print(f"[Hata] {e}")
        time.sleep(SCAN_INTERVAL)

if __name__ == "__main__":
    main()
