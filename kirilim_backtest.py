import os
import requests
import ccxt
import pandas as pd
import time

# --- TANIMLAMALAR & TELEGRAM BİLGİLERİ ---
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
SYMBOL = "BTC/USDT"
TIMEFRAME = "1h"
LOOKBACK = 10     # Kaç mumun ortalamasına bakılacak
MULTIPLIER = 2.0  # Önceki mumların ortalamasının kaç katı olacak
TP_PERCENT = 0.05 # %5 Kâr Al

exchange = ccxt.binance()

def send_telegram_msg(msg):
    if TELEGRAM_TOKEN and CHAT_ID:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        payload = {"chat_id": CHAT_ID, "text": msg, "parse_mode": "Markdown"}
        try:
            requests.post(url, json=payload)
        except Exception as e:
            print(f"Telegram hatası: {e}")

def run_strategy():
    # Borsadan son mum verilerini çek
    bars = exchange.fetch_ohlcv(SYMBOL, timeframe=TIMEFRAME, limit=LOOKBACK + 5)
    df = pd.DataFrame(bars, columns=['time', 'open', 'high', 'low', 'close', 'volume'])
    
    # Mum Gövde Boyutunu Hesapla (Abs Close - Open)
    df['body_size'] = (df['close'] - df['open']).abs()
    
    # Yeşil mum kontrolü
    df['is_green'] = df['close'] > df['open']
    
    # Önceki N mumun ortalama gövde boyutu
    df['avg_prior_body'] = df['body_size'].shift(1).rolling(window=LOOKBACK).mean()
    
    # Hacimli/Büyük Yeşil Mum Şartı
    df['is_big_green'] = df['is_green'] & (df['body_size'] >= df['avg_prior_body'] * MULTIPLIER)
    
    # Arda arda 3 tane büyük yeşil mum şartı (Son 3 mum)
    c3 = df['is_big_green'].iloc[-1]
    c2 = df['is_big_green'].iloc[-2]
    c1 = df['is_big_green'].iloc[-3]
    
    if c1 and c2 and c3:
        entry_price = df['close'].iloc[-1]
        # İlk yeşil mumun altı (3 mum önceki low)
        stop_loss = df['low'].iloc[-3]
        take_profit = entry_price * (1 + TP_PERCENT)
        
        msg = (
            f"🚀 *SİNYAL YAKALANDI! ({SYMBOL})*\n\n"
            f"Üst üste {MULTIPLIER}x büyüklükte 3 Yeşil Mum oluştu.\n\n"
            f"📍 *Giriş Fiyatı:* `{entry_price:.2f}`\n"
            f"🛑 *Stop Loss (1. Mum Low):* `{stop_loss:.2f}`\n"
            f"🎯 *Take Profit (%5):* `{take_profit:.2f}`"
        )
        print("Sinyal bulundu, Telegram'a gönderiliyor...")
        send_telegram_msg(msg)

if __name__ == "__main__":
    print("Bot çalışıyor...")
    while True:
        try:
            run_strategy()
        except Exception as e:
            print(f"Hata oluştu: {e}")
        # Saatlik mum kontrolü için örneğin her 5 dakikada bir kontrol et
        time.sleep(300)
