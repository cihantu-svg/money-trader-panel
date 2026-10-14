import os
import time
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
import pandas as pd
import requests
from requests.adapters import HTTPAdapter

# ==============================================================================
# YAPILANDIRMA (ENV İsimleri Panelle Eşitlendi)
# ==============================================================================
TIMEFRAME = os.getenv("TIMEFRAME", "15m")                    
SCAN_INTERVAL = int(os.getenv("SCAN_INTERVAL", 300))         # 5 dakika (300 saniye)
MAX_WORKERS = int(os.getenv("MAX_WORKERS", 8))               
DEBUG_LOG = os.getenv("DEBUG_LOG", "false").lower() == "true"

# Pine Script Kırılım Parametreleri
BO_LEN = 20                 
BO_BUFFER_PCT = 0.5         
VOL_MULT = 2.0              
RSI_LEN = 14                
RSI_BULL = 60               
RSI_BEAR = 40               
SMA_LEN = 100               
TOUCH_TOLERANCE_PCT = 0.4   # SMA'ya temas için izin verilen tolerans yüzdesi (%)

# GÜRÜLTÜ ENGELLEYİCİ: Kırılım mumu min %3 gövde büyüklüğünde olmalı
MIN_CANDLE_BODY_PCT = 3.0   

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

def calculate_indicators(df):
    df = df.copy()
    
    # 1. SMA 100
    df['sma100'] = df['close'].rolling(window=SMA_LEN).mean()
    
    # 2. Kırılım Seviyeleri
    df['resistance'] = df['high'].shift(1).rolling(window=BO_LEN).max()
    df['support'] = df['low'].shift(1).rolling(window=BO_LEN).min()
    
    # 3. Hacim Ortalaması (20 periyot SMA)
    df['vol_sma'] = df['volume'].rolling(window=20).mean()
    
    # 4. RSI (14)
    delta = df['close'].diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=RSI_LEN).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=RSI_LEN).mean()
    rs = gain / loss
    df['rsi'] = 100 - (100 / (1 + rs))
    
    # 5. MACD (12, 26, 9)
    exp1 = df['close'].ewm(span=12, adjust=False).mean()
    exp2 = df['close'].ewm(span=26, adjust=False).mean()
    df['macd_line'] = exp1 - exp2
    df['signal_line'] = df['macd_line'].ewm(span=9, adjust=False).mean()
    df['macd_hist'] = df['macd_line'] - df['signal_line']
    
    return df

def analyze_symbol(symbol: str):
    try:
        res = session.get(
            f"{API_BASE}/fapi/v1/klines",
            params={"symbol": symbol, "interval": TIMEFRAME, "limit": 130},
            timeout=10,
        )
        if res.status_code in (418, 429):
            return None
        if res.status_code != 200:
            return None

        klines = res.json()
        if not isinstance(klines, list) or len(klines) < 120:
            return None

        df = pd.DataFrame(klines, columns=[
            'open_time', 'open', 'high', 'low', 'close', 'volume',
            'close_time', 'quote_volume', 'trades', 'taker_buy_base',
            'taker_buy_quote', 'ignore'
        ]).iloc[:, :11].astype(float)

        now_ms = int(time.time() * 1000)
        df = df[df["close_time"] < now_ms].reset_index(drop=True)

        if len(df) < SMA_LEN + BO_LEN:
            return None

        df = calculate_indicators(df)
        
        m1 = df.iloc[-2]  
        m = df.iloc[-1]   

        # --- 1. MUM BOYU / GÖVDE KONTROLÜ (Min %3 Şartı) ---
        candle_change_pct = abs((m['close'] - m['open']) / m['open']) * 100.0
        if candle_change_pct < MIN_CANDLE_BODY_PCT:
            return None

        # --- 2. KIRILIM KOŞULLARI ---
        resistance_target = m['resistance'] * (1 + BO_BUFFER_PCT / 100)
        support_target = m['support'] * (1 - BO_BUFFER_PCT / 100)

        breakout_up = m['close'] > resistance_target and m1['close'] <= m['resistance']
        breakout_down = m['close'] < support_target and m1['close'] >= m['support']

        vol_bullish = m['volume'] > (m['vol_sma'] * VOL_MULT)
        vol_bearish = m['volume'] > (m['vol_sma'] * VOL_MULT)

        mom_bullish = m['rsi'] > RSI_BULL and m['macd_hist'] > 0 and m['macd_line'] > m['signal_line']
        mom_bearish = m['rsi'] < RSI_BEAR and m['macd_hist'] < 0 and m['macd_line'] < m['signal_line']

        # --- 3. SMA 100 TEMAS (TOUCH) ŞARTI ---
        sma_val = m['sma100']
        touch_threshold = sma_val * (TOUCH_TOLERANCE_PCT / 100.0)
        is_touching_sma = (m['low'] <= sma_val + touch_threshold) and (m['high'] >= sma_val - touch_threshold)

        signal_buy = breakout_up and vol_bullish and mom_bullish and is_touching_sma
        signal_sell = breakout_down and vol_bearish and mom_bearish and is_touching_sma

        if not (signal_buy or signal_sell):
            return None

        direction = "LONG" if signal_buy else "SHORT"

        return {
            "symbol": symbol,
            "direction": direction,
            "price": m['close'],
            "rsi": m['rsi'],
            "sma100": sma_val,
            "candle_change": candle_change_pct,
            "open_time": int(m['open_time'])
        }

    except Exception:
        return None

def run_scanner():
    now = datetime.now(timezone.utc).strftime("%H:%M:%S")
    symbols = get_usdt_symbols()
    print(f"\n[{now} UTC] 🔍 Tarama Başlatıldı ({TIMEFRAME}) | Toplam Sembol: {len(symbols)}")
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
        badge = "🟢 15m KIRILIM AL (MIN %3 MUM + SMA 100)" if is_long else "🔴 15m KIRILIM SAT (MIN %3 MUM + SMA 100)"
        
        msg = (
            f"========================================\n"
            f"{badge}\n"
            f"Sembol: {sym.replace('USDT', '')}/USDT\n"
            f"Zaman Dilimi: {TIMEFRAME} (5dk Tarama)\n"
            f"Fiyat: ${s['price']}\n"
            f"Mum Değişimi: %{s['candle_change']:.2f}\n"
            f"SMA 100 Seviyesi: ${s['sma100']:.4f}\n"
            f"RSI: {s['rsi']:.1f}\n"
            f"========================================"
        )
        print(msg)
        send_telegram_alert(msg)

def main():
    print(f"🚀 PINE SCRIPT KIRILIM + SMA 100 TEMAS TARAYICI AKTİF ({TIMEFRAME} - Min %3 Mum Boyu Şartı)")
    while True:
        try:
            run_scanner()
        except Exception as e:
            print(f"[Hata] {e}")
        time.sleep(SCAN_INTERVAL)

if __name__ == "__main__":
    main()
