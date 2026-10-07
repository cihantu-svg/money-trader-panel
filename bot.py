import os
import time
import threading
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
import pandas as pd
import requests

# ==============================================================================
# YAPILANDIRMA VE EŞİK DEĞERLERİ (İster buradan ister .env/ortam değişkeninden değiştirin)
# ==============================================================================
TIMEFRAME = os.getenv("TIMEFRAME", "15m")                  # Tarama zaman dilimi (5m, 15m, 1h)
MIN_VOL_USDT = float(os.getenv("MIN_VOL_USDT", 5000000))   # Minimum mum hacmi ($5M USDT)
MIN_VOL_MULT = float(os.getenv("MIN_VOL_MULT", 10.0))      # Son 20 mum ortalamasına göre min. hacim patlaması katı (10x)
MIN_MOVE_PCT = float(os.getenv("MIN_MOVE_PCT", 5.0))       # Minimum mum gövde değişimi (%)
SCAN_INTERVAL = int(os.getenv("SCAN_INTERVAL", 180))       # Taramalar arası bekleme süresi (Saniye)
MAX_WORKERS = int(os.getenv("MAX_WORKERS", 10))            # Eşzamanlı istek sayısı (Thread sayısı)

# Telegram Ayarları (İsteğe bağlı - Boş bırakılırsa Telegram'a mesaj atmaz)
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

API_BASE = "https://fapi.binance.com"


def send_telegram_alert(message: str):
    """
    Telegram kanalına veya kullanıcısına anlık bildirim gönderir.
    """
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML"
    }
    try:
        requests.post(url, json=payload, timeout=5)
    except Exception as e:
        print(f"[Hata] Telegram bildirimi gönderilemedi: {e}")


def get_usdt_symbols():
    """
    Binance Futures üzerindeki aktif USDT çiftlerini çeker.
    """
    try:
        response = requests.get(f"{API_BASE}/fapi/v1/ticker/24hr", timeout=10)
        tickers = response.json()
        return [t['symbol'] for t in tickers if t['symbol'].endswith('USDT')]
    except Exception as e:
        print(f"[Hata] Ticker verisi alınamadı: {e}")
        return []


def analyze_symbol(symbol: str):
    """
    Tek bir coin için Binance Futures mum verilerini ve Net Delta'yı analiz eder.
    """
    try:
        url = f"{API_BASE}/fapi/v1/klines"
        params = {
            "symbol": symbol,
            "interval": TIMEFRAME,
            "limit": 22
        }
        res = requests.get(url, params=params, timeout=5)
        klines = res.json()

        if not klines or len(klines) < 21:
            return None

        # pandas DataFrame ile veriyi temizleme ve hizalama
        df = pd.DataFrame(klines, columns=[
            'open_time', 'open', 'high', 'low', 'close', 'volume',
            'close_time', 'quote_volume', 'trades', 'taker_buy_base',
            'taker_buy_quote', 'ignore'
        ])

        # Canlı mumu pas geçip son tamamlanan 21 mumu analiz et
        closed_df = df.iloc[:-1].copy()
        
        # Sayısal dönüştürmeler
        closed_df['open'] = closed_df['open'].astype(float)
        closed_df['high'] = closed_df['high'].astype(float)
        closed_df['low'] = closed_df['low'].astype(float)
        closed_df['close'] = closed_df['close'].astype(float)
        closed_df['quote_volume'] = closed_df['quote_volume'].astype(float)
        closed_df['taker_buy_quote'] = closed_df['taker_buy_quote'].astype(float)

        last_candle = closed_df.iloc[-1]
        
        open_p = last_candle['open']
        close_p = last_candle['close']
        volume = last_candle['quote_volume']
        taker_buy = last_candle['taker_buy_quote']

        # 1. ŞART: Minimum Mum Hacmi
        if volume < MIN_VOL_USDT:
            return None

        # 2. ŞART: Minimum Mum Gövde Değişimi (%)
        change_pct = (abs(close_p - open_p) / open_p) * 100
        if change_pct < MIN_MOVE_PCT:
            return None

        # 3. ŞART: Son 20 mum ortalamasına göre Hacim Patlaması (x)
        prev_20_vol = closed_df['quote_volume'].iloc[-21:-1].mean()
        if prev_20_vol == 0:
            return None

        multiplier = volume / prev_20_vol
        if multiplier < MIN_VOL_MULT:
            return None

        # Order Flow & Net Delta Hesabı (Taker Buy vs Taker Sell)
        taker_sell = volume - taker_buy
        net_delta = taker_buy - taker_sell
        direction = "LONG" if net_delta > 0 else "SHORT"

        return {
            "symbol": symbol,
            "price": close_p,
            "change_pct": change_pct,
            "volume": volume,
            "multiplier": multiplier,
            "net_delta": net_delta,
            "direction": direction
        }

    except Exception:
        return None


def run_scanner():
    """
    Tüm USDT çiftlerini paralel ThreadPool ile hızlıca tarar.
    """
    now = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"\n[{now} UTC] 🐋 Binance Futures Balina Taraması Başlatıldı...")

    symbols = get_usdt_symbols()
    if not symbols:
        print("[!] Taranacak sembol bulunamadı.")
        return

    detected_whales = []

    # Çoklu izlek (Thread) kullanarak Binance isteklerini paralel yap
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        future_to_symbol = {executor.submit(analyze_symbol, sym): sym for sym in symbols}
        for future in as_completed(future_to_symbol):
            result = future.result()
            if result:
                detected_whales.append(result)

    print(f"[Tamamlandı] Taranan Çift: {len(symbols)} | Yakalanan Balina: {len(detected_whales)}")

    # Ekrana loglama ve bildirim gönderme
    for w in detected_whales:
        is_long = w['direction'] == 'LONG'
        badge = "🟢 AGRESİF BALİNA ALIMI (LONG)" if is_long else "🔴 AGRESİF BALİNA SATIŞI (SHORT)"
        
        log_msg = (
            f"========================================\n"
            f"{badge}\n"
            f"Sembol: {w['symbol'].replace('USDT', '')}/USDT\n"
            f"Kapanış Fiyatı: ${w['price']}\n"
            f"Mum Hacmi: ${w['volume'] / 1000000:.2f}M\n"
            f"Hacim Patlaması: {w['multiplier']:.1f}x (Son 20 Ort.)\n"
            f"Mum Değişimi: %{w['change_pct']:.2f}\n"
            f"Net Delta Baskısı: ${w['net_delta'] / 1000000:.2f}M\n"
            f"Zaman: {datetime.now(timezone.utc).strftime('%H:%M:%S UTC')}\n"
            f"========================================"
        )
        print(log_msg)
        send_telegram_alert(log_msg)


def main():
    """
    Sürekli tarama yapacak döngü mekanizması
    """
    print("🐋 WHALE TRACKER BOTU BAŞLATILDI")
    print(f"Parametreler: Min Hacim: ${MIN_VOL_USDT/1e6}M | Min Kat: {MIN_VOL_MULT}x | Min Değişim: %{MIN_MOVE_PCT}")
    
    while True:
        try:
            run_scanner()
        except Exception as e:
            print(f"[Sistem Hatası] Tarama döngüsünde hata: {e}")
        
        time.sleep(SCAN_INTERVAL)


if __name__ == "__main__":
    main()
