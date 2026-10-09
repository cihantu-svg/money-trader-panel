import os
import time
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
import pandas as pd
import requests

# ==============================================================================
# YAPILANDIRMA VE EŞİK DEĞERLERİ (ENV Üzerinden Okunur)
# ==============================================================================
TIMEFRAME = os.getenv("TIMEFRAME", "15m")                  # Tarama zaman dilimi
ADX_PERIOD = int(os.getenv("ADX_PERIOD", 14))              # ADX / DI Periyodu
EMA_PERIOD = int(os.getenv("EMA_PERIOD", 100))             # EMA Periyodu (EMA 100)
MIN_CANDLE_PCT = float(os.getenv("MIN_CANDLE_PCT", 3.0))    # Min Mum Gövde Boyu (%) -> Önerilen: %3.0 (ENV'den değiştirilebilir)
ADX_THRESHOLD = float(os.getenv("ADX_THRESHOLD", 15.0))    # Min ADX trend gücü
SCAN_INTERVAL = int(os.getenv("SCAN_INTERVAL", 180))       # Taramalar arası bekleme (Saniye)
MAX_WORKERS = int(os.getenv("MAX_WORKERS", 10))            # Thread sayısı
DEBUG_LOG = os.getenv("DEBUG_LOG", "false").lower() == "true" # Elenme logları (Default: kapalı)

# Telegram Ayarları
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


def calculate_indicators(df, adx_period=14, ema_period=100):
    df = df.copy()
    
    # 1. EMA 100 Hesabı
    df['ema100'] = df['close'].ewm(span=ema_period, adjust=False).mean()

    # 2. ADX / DI Hesabı (Wilder's DMI)
    df['up_move'] = df['high'] - df['high'].shift(1)
    df['down_move'] = df['low'].shift(1) - df['low']
    
    df['plus_dm'] = 0.0
    df['minus_dm'] = 0.0
    
    df.loc[(df['up_move'] > df['down_move']) & (df['up_move'] > 0), 'plus_dm'] = df['up_move']
    df.loc[(df['down_move'] > df['up_move']) & (df['down_move'] > 0), 'minus_dm'] = df['down_move']
    
    df['tr'] = pd.concat([
        df['high'] - df['low'],
        (df['high'] - df['close'].shift(1)).abs(),
        (df['low'] - df['close'].shift(1)).abs()
    ], axis=1).max(axis=1)
    
    tr_smooth = df['tr'].ewm(alpha=1/adx_period, adjust=False).mean()
    plus_dm_smooth = df['plus_dm'].ewm(alpha=1/adx_period, adjust=False).mean()
    minus_dm_smooth = df['minus_dm'].ewm(alpha=1/adx_period, adjust=False).mean()
    
    df['plus_di'] = 100 * (plus_dm_smooth / tr_smooth)
    df['minus_di'] = 100 * (minus_dm_smooth / tr_smooth)
    
    dx = 100 * ((df['plus_di'] - df['minus_di']).abs() / (df['plus_di'] + df['minus_di']))
    df['adx'] = dx.ewm(alpha=1/adx_period, adjust=False).mean()
    
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
            "limit": EMA_PERIOD + 30
        }
        res = requests.get(url, params=params, timeout=5)
        klines = res.json()

        if not klines or len(klines) < (EMA_PERIOD + 10):
            return None

        df = pd.DataFrame(klines, columns=[
            'open_time', 'open', 'high', 'low', 'close', 'volume',
            'close_time', 'quote_volume', 'trades', 'taker_buy_base',
            'taker_buy_quote', 'ignore'
        ])

        # Canlı mumu çıkarıp kapalı mumları al
        closed_df = df.iloc[:-1].copy()
        
        for col in ['open', 'high', 'low', 'close', 'quote_volume']:
            closed_df[col] = closed_df[col].astype(float)

        df_ind = calculate_indicators(closed_df, adx_period=ADX_PERIOD, ema_period=EMA_PERIOD)

        curr = df_ind.iloc[-1]
        
        open_p = curr['open']
        close_p = curr['close']
        candle_body_pct = (abs(close_p - open_p) / open_p) * 100.0
        adx_val = curr['adx']
        ema_val = curr['ema100']

        # ----------------------------------------------------------------------
        # SON 3 MUM İÇERİSİNDE KESİŞİM ARAMA (Sinyal Kaçırmama Penceresi)
        # ----------------------------------------------------------------------
        has_bull_cross = False
        has_bear_cross = False

        for i in range(1, 4):  # Son 3 mumu tara
            c_row = df_ind.iloc[-i]
            p_row = df_ind.iloc[-i-1]
            
            # Long Kesişim: +DI, -DI'yi yukarı kesti mi?
            if (p_row['plus_di'] <= p_row['minus_di']) and (c_row['plus_di'] > c_row['minus_di']):
                has_bull_cross = True
            # Short Kesişim: -DI, +DI'yi yukarı kesti mi?
            if (p_row['minus_di'] <= p_row['plus_di']) and (c_row['minus_di'] > c_row['plus_di']):
                has_bear_cross = True

        # ŞARTLAR:
        # 1. Son 3 mumda kesişim gerçekleşmiş olması
        # 2. Fiyatın EMA100'ün doğru tarafında bulunması
        # 3. Mum gövdesinin belirtilen eşiği karşılaması
        # 4. ADX trend gücünün eşiği geçmesi
        is_long = has_bull_cross and (close_p >= ema_val) and (candle_body_pct >= MIN_CANDLE_PCT) and (adx_val >= ADX_THRESHOLD)
        is_short = has_bear_cross and (close_p <= ema_val) and (candle_body_pct >= MIN_CANDLE_PCT) and (adx_val >= ADX_THRESHOLD)

        # Debug loglar istenirse aktifleşir
        if (has_bull_cross or has_bear_cross) and DEBUG_LOG and not (is_long or is_short):
            fail_reasons = []
            if candle_body_pct < MIN_CANDLE_PCT:
                fail_reasons.append(f"Mum Yetersiz (%{candle_body_pct:.2f} < %{MIN_CANDLE_PCT})")
            if adx_val < ADX_THRESHOLD:
                fail_reasons.append(f"ADX Zayıf ({adx_val:.1f} < {ADX_THRESHOLD})")
            if has_bull_cross and close_p < ema_val:
                fail_reasons.append("EMA100 Altında")
            if has_bear_cross and close_p > ema_val:
                fail_reasons.append("EMA100 Üstünde")
            print(f"⚠️ [{symbol}] Kesişim Var Ama Elendi -> " + " | ".join(fail_reasons))

        if not (is_long or is_short):
            return None

        direction = "LONG" if is_long else "SHORT"

        return {
            "symbol": symbol,
            "price": close_p,
            "ema100": ema_val,
            "candle_body_pct": candle_body_pct,
            "adx": adx_val,
            "plus_di": curr['plus_di'],
            "minus_di": curr['minus_di'],
            "volume": curr['quote_volume'],
            "direction": direction
        }

    except Exception:
        return None


def run_scanner():
    now = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"\n[{now} UTC] 🔍 ADX/EMA Breakout Taraması Başlatıldı (Min Mum: %{MIN_CANDLE_PCT})...")

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

    print(f"[Tamamlandı] Taranan Çift: {len(symbols)} | Tespit Edilen Sinyal: {len(detected_signals)}")

    for s in detected_signals:
        is_long = s['direction'] == 'LONG'
        badge = "🟢 A+ LONG SİNYALİ" if is_long else "🔴 A+ SHORT SİNYALİ"
        
        log_msg = (
            f"========================================\n"
            f"{badge}\n"
            f"Sembol: {s['symbol'].replace('USDT', '')}/USDT\n"
            f"Kapanış Fiyatı: ${s['price']}\n"
            f"EMA 100: ${s['ema100']:.4f}\n"
            f"Mum Gövde Boyu: %{s['candle_body_pct']:.2f}\n"
            f"ADX Gücü: {s['adx']:.1f}\n"
            f"+DI: {s['plus_di']:.1f} | -DI: {s['minus_di']:.1f}\n"
            f"========================================"
        )
        print(log_msg)
        send_telegram_alert(log_msg)


def main():
    print("🚀 OPTİMİZE ADX/DI BREAKOUT SCANNER BAŞLATILDI")
    print(f"ENV Ayarları -> Min Mum: %{MIN_CANDLE_PCT} | ADX Eşik: >{ADX_THRESHOLD} | EMA: {EMA_PERIOD} | Debug Log: {DEBUG_LOG}")
    while True:
        try:
            run_scanner()
        except Exception as e:
            print(f"[Sistem Hatası] Tarama hatası: {e}")
        time.sleep(SCAN_INTERVAL)


if __name__ == "__main__":
    main()
