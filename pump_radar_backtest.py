#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PUMP RADAR v2.1 - Backtest & Live Scanner (Londra Saati)
Düzeltilmiş versiyon:
1. Thread-safe event yönetimi (threading.Lock)
2. Faz B repaint koruması (son canlı mum atılır)
3. Symbol listesi önbellekleme (rate limit koruma)
4. Backtest motoru + CSV çıktısı
5. Telegram entegrasyonu
6. Tüm zamanlar LONDRA SAATİ (UTC+0/BST otomatik)
"""

import threading
import time
import json
import csv
import os
import sys
import logging
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import deque
import requests
import numpy as np
import pandas as pd
import pytz

# ==================== ZAMAN AYARI (LONDRA) ====================
LONDON_TZ = pytz.timezone('Europe/London')
UTC_TZ = pytz.UTC

def to_london(dt):
    """Herhangi bir timezone'lu datetime'ı Londra'ya çevir"""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = UTC_TZ.localize(dt)
    return dt.astimezone(LONDON_TZ)

def format_london(dt, fmt='%Y-%m-%d %H:%M:%S %Z'):
    """Datetime'ı Londra saat formatında string yap"""
    if dt is None:
        return 'N/A'
    dt_lon = to_london(dt)
    return dt_lon.strftime(fmt)

def now_london():
    """Şu anki Londra saati"""
    return datetime.now(LONDON_TZ)

def parse_london(date_str):
    """'YYYY-MM-DD' stringini Londra midnight'a çevir"""
    dt = datetime.strptime(date_str, '%Y-%m-%d')
    return LONDON_TZ.localize(dt)

# ==================== LOGGING (Londra Saati) ====================
class LondonFormatter(logging.Formatter):
    def formatTime(self, record, datefmt=None):
        dt = datetime.fromtimestamp(record.created, LONDON_TZ)
        if datefmt:
            return dt.strftime(datefmt)
        return dt.strftime('%Y-%m-%d %H:%M:%S %Z')

handler = logging.StreamHandler(sys.stdout)
handler.setFormatter(LondonFormatter('%(asctime)s | %(levelname)s | %(message)s'))

file_handler = logging.FileHandler('pump_radar.log', encoding='utf-8')
file_handler.setFormatter(LondonFormatter('%(asctime)s | %(levelname)s | %(message)s'))

logging.basicConfig(level=logging.INFO, handlers=[handler, file_handler])
logger = logging.getLogger('PUMP_RADAR')

# ==================== KONFİGÜRASYON ====================
CONFIG = {
    'API_BASE': 'https://fapi.binance.com',
    'MAX_COINS': 150,
    'TIMEFRAMES': {
        '1m': {'interval': '1m', 'limit': 150, 'warmup': 90},
        '5m': {'interval': '5m', 'limit': 100, 'warmup': 20},
    },
    'SCAN_INTERVAL': 60,
    'MAX_WORKERS': 8,
    'TELEGRAM_BOT_TOKEN': os.getenv('TELEGRAM_BOT_TOKEN', 'YOUR_BOT_TOKEN'),
    'TELEGRAM_CHAT_ID': os.getenv('TELEGRAM_CHAT_ID', 'YOUR_CHAT_ID'),
    'BACKTEST_MODE': True,
    'BACKTEST_START': '2024-01-01',
    'BACKTEST_END': '2024-03-01',
    'CSV_OUTPUT': 'pump_radar_backtest.csv',
    'SYMBOL_CACHE_TTL': 3600,
    'SIGNAL_THRESHOLD_PHASE_A': 40,
    'SIGNAL_THRESHOLD_PHASE_B': 70,
    'MAX_BACKTEST_SIGNALS_PER_SYMBOL': 500,
}

# ==================== GLOBAL DEĞİŞKENLER (Thread-Safe) ====================
event_lock = threading.Lock()
pending_events = {}
last_processed_event_time = {}
_cached_symbols = None
_cache_time = 0

# ==================== BINANCE API CLIENT ====================
class BinanceAPI:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({'User-Agent': 'PUMP-RADAR/2.1'})
        self.base = CONFIG['API_BASE']
        self.last_request_time = 0
        self.min_interval = 0.05

    def _rate_limit(self):
        now = time.time()
        elapsed = now - self.last_request_time
        if elapsed < self.min_interval:
            time.sleep(self.min_interval - elapsed)
        self.last_request_time = time.time()

    def get(self, endpoint, params=None):
        self._rate_limit()
        url = f"{self.base}{endpoint}"
        try:
            resp = self.session.get(url, params=params, timeout=15)
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.RequestException as e:
            logger.error(f"API Error {endpoint}: {e}")
            return None

    def get_exchange_info(self):
        return self.get('/fapi/v1/exchangeInfo')

    def get_klines(self, symbol, interval, limit, end_time=None):
        params = {
            'symbol': symbol,
            'interval': interval,
            'limit': limit
        }
        if end_time:
            params['endTime'] = int(end_time)
        data = self.get('/fapi/v1/klines', params)
        if not data or not isinstance(data, list):
            return None

        df = pd.DataFrame(data, columns=[
            'open_time', 'open', 'high', 'low', 'close', 'volume',
            'close_time', 'quote_volume', 'trades', 'taker_buy_base',
            'taker_buy_quote', 'ignore'
        ])
        # Binance UTC döndürür → Londra'ya çevir
        df['open_time'] = pd.to_datetime(df['open_time'], unit='ms', utc=True).dt.tz_convert(LONDON_TZ)
        df['close_time'] = pd.to_datetime(df['close_time'], unit='ms', utc=True).dt.tz_convert(LONDON_TZ)
        for col in ['open', 'high', 'low', 'close', 'volume', 'quote_volume']:
            df[col] = pd.to_numeric(df[col], errors='coerce')
        return df.dropna()

api = BinanceAPI()

# ==================== THREAD-SAFE SYMBOL CACHE ====================
def get_symbols():
    global _cached_symbols, _cache_time

    with event_lock:
        if _cached_symbols and (time.time() - _cache_time) < CONFIG['SYMBOL_CACHE_TTL']:
            logger.debug("Sembol önbellekten döndürüldü")
            return _cached_symbols

    logger.info("ExchangeInfo çekiliyor...")
    info = api.get_exchange_info()
    if not info:
        logger.error("ExchangeInfo alınamadı")
        return []

    syms = []
    for s in info.get('symbols', []):
        if (s.get('status') == 'TRADING' and 
            s.get('contractType') == 'PERPETUAL' and
            s['symbol'].endswith('USDT')):
            syms.append(s['symbol'])

    syms = sorted(syms)[:CONFIG['MAX_COINS']]

    with event_lock:
        _cached_symbols = syms
        _cache_time = time.time()

    logger.info(f"{len(syms)} sembol yüklendi")
    return syms

# ==================== VERİ ÇEKME (Repaint Korumalı + Londra) ====================
def get_klines_closed(symbol, interval, limit, end_time=None):
    df = api.get_klines(symbol, interval, limit + 1, end_time)
    if df is None or len(df) < 2:
        return None
    return df.iloc[:-1].reset_index(drop=True)

def get_klines_window(symbol, interval, limit, end_time=None):
    df = api.get_klines(symbol, interval, limit, end_time)
    if df is None or len(df) < 2:
        return None
    return df

# ==================== TEKNİK İNDİKATÖRLER ====================
def wma(series, period):
    weights = np.arange(1, period + 1)
    def _wma(x):
        return np.dot(x, weights) / weights.sum()
    return series.rolling(window=period).apply(_wma, raw=True)

def ema(series, period):
    return series.ewm(span=period, adjust=False).mean()

def cmo_custom(close, period=14):
    diff = close.diff()
    up = diff.where(diff > 0, 0.0)
    down = (-diff).where(diff < 0, 0.0)
    sum_up = up.rolling(window=period).sum()
    sum_down = down.rolling(window=period).sum()
    denom = sum_up + sum_down
    cmo = np.where(denom == 0, 0.0, 100.0 * (sum_up - sum_down) / denom)
    return pd.Series(cmo, index=close.index)

def squeeze_momentum(df, bb_len=20, bb_mult=2.0, kc_len=20, kc_mult=1.5):
    close = df['close']
    high = df['high']
    low = df['low']

    basis = close.rolling(window=bb_len).mean()
    dev = bb_mult * close.rolling(window=bb_len).std()
    upper_bb = basis + dev
    lower_bb = basis - dev

    ma = close.rolling(window=kc_len).mean()
    range_val = high - low
    val = kc_mult * range_val.rolling(window=kc_len).mean()
    upper_kc = ma + val
    lower_kc = ma - val

    squeeze_on = (lower_bb > lower_kc) & (upper_bb < upper_kc)

    highest = high.rolling(window=kc_len).max()
    lowest = low.rolling(window=kc_len).min()
    avg = (highest + lowest) / 2
    momentum = (close - avg).rolling(window=3).mean()

    return squeeze_on, momentum

def compute_sup_res(df, left_bars=5, right_bars=5):
    highs = df['high'].values
    lows = df['low'].values
    n = len(df)

    pivot_high = np.zeros(n, dtype=bool)
    pivot_low = np.zeros(n, dtype=bool)

    for i in range(left_bars, n - right_bars):
        left_max = np.max(highs[i-left_bars:i])
        right_max = np.max(highs[i+1:i+right_bars+1])
        if highs[i] >= left_max and highs[i] >= right_max:
            pivot_high[i] = True

        left_min = np.min(lows[i-left_bars:i])
        right_min = np.min(lows[i+1:i+right_bars+1])
        if lows[i] <= left_min and lows[i] <= right_min:
            pivot_low[i] = True

    res_levels = df.loc[pivot_high, 'high'].tail(3).values
    sup_levels = df.loc[pivot_low, 'low'].tail(3).values

    res = float(res_levels[-1]) if len(res_levels) > 0 else None
    sup = float(sup_levels[-1]) if len(sup_levels) > 0 else None

    return sup, res

# ==================== SİNYAL MOTORU ====================
def check_divergence(price, indicator, lookback=20):
    if len(price) < lookback or len(indicator) < lookback:
        return None

    price_vals = price.iloc[-lookback:].values
    ind_vals = indicator.iloc[-lookback:].values
    x = np.arange(len(price_vals))

    price_trend = np.polyfit(x, price_vals, 1)[0]
    ind_trend = np.polyfit(x, ind_vals, 1)[0]

    if price_trend < -0.001 and ind_trend > 0.001:
        return 'bullish'
    if price_trend > 0.001 and ind_trend < -0.001:
        return 'bearish'
    return None

def calculate_score(df_1m, df_5m=None):
    if len(df_1m) < 90:
        return 0, {}

    close = df_1m['close']
    volume = df_1m['volume']

    ema9 = ema(close, 9)
    ema21 = ema(close, 21)
    ema50 = ema(close, 50)

    volume_ma = volume.rolling(20).mean()
    current_vol = volume.iloc[-1]
    avg_vol = volume_ma.iloc[-1]
    high_volume = current_vol > avg_vol * 1.5 if not pd.isna(avg_vol) else False

    curr_close = close.iloc[-1]
    ema_break = (curr_close > ema9.iloc[-1] > ema21.iloc[-1] > ema50.iloc[-1])

    squeeze_on, momentum = squeeze_momentum(df_1m)
    squeeze_fired = False
    if len(squeeze_on) >= 2:
        squeeze_fired = bool(squeeze_on.iloc[-2]) and not bool(squeeze_on.iloc[-1])

    cmo = cmo_custom(close, 14)
    div = check_divergence(close, cmo)

    score = 0
    details = {}

    if ema_break and high_volume:
        score += 40
        details['ema_break'] = True
        details['volume_ratio'] = round(current_vol / avg_vol, 2) if avg_vol > 0 else 0

    if squeeze_fired:
        score += 30
        details['squeeze'] = True

    if div == 'bullish':
        score += 30
        details['divergence'] = True

    return score, details

# ==================== İKİ FAZLI TARAMA ====================
def phase_a_scan(symbol, end_time=None):
    try:
        limit = CONFIG['TIMEFRAMES']['1m']['limit']
        df = get_klines_closed(symbol, '1m', limit, end_time)
        if df is None or len(df) < 90:
            return None

        score, details = calculate_score(df)
        if score >= CONFIG['SIGNAL_THRESHOLD_PHASE_A']:
            return {
                'symbol': symbol,
                'score': score,
                'details': details,
                'timestamp': df['close_time'].iloc[-1],
                'open_time': df['open_time'].iloc[-1],
                'close': float(df['close'].iloc[-1]),
                'volume': float(df['volume'].iloc[-1])
            }
        return None
    except Exception as e:
        logger.error(f"Phase A hata {symbol}: {e}")
        return None

def phase_b_confirm(symbol, timestamp, end_time=None):
    try:
        limit_1m = CONFIG['TIMEFRAMES']['1m']['limit']
        df1m_raw = get_klines_window(symbol, '1m', limit_1m, end_time)
        if df1m_raw is None or len(df1m_raw) < 2:
            return None

        df1m = df1m_raw.iloc[:-1].reset_index(drop=True)

        if len(df1m) < 90:
            return None

        limit_5m = CONFIG['TIMEFRAMES']['5m']['limit']
        df5m = get_klines_closed(symbol, '5m', limit_5m, end_time)
        if df5m is None or len(df5m) < 20:
            df5m = None

        score, details = calculate_score(df1m, df5m)
        sup, res = compute_sup_res(df1m)

        if score >= CONFIG['SIGNAL_THRESHOLD_PHASE_B'] and details.get('ema_break') and details.get('squeeze'):
            return {
                'symbol': symbol,
                'score': score,
                'details': details,
                'support': sup,
                'resistance': res,
                'timestamp': df1m['close_time'].iloc[-1],
                'close': float(df1m['close'].iloc[-1]),
                'volume': float(df1m['volume'].iloc[-1]),
                'open_time': df1m['open_time'].iloc[-1]
            }
        return None
    except Exception as e:
        logger.error(f"Phase B hata {symbol}: {e}")
        return None

# ==================== THREAD-SAFE EVENT YÖNETİMİ ====================
def check_new_event(symbol, open_time):
    with event_lock:
        if last_processed_event_time.get(symbol) == open_time:
            return None
        last_processed_event_time[symbol] = open_time

        if symbol in pending_events:
            return None

        return True

def set_pending_event(symbol, event):
    with event_lock:
        pending_events[symbol] = event

def clear_pending_event(symbol):
    with event_lock:
        if symbol in pending_events:
            del pending_events[symbol]

def cleanup_old_events(max_age_hours=168):
    cutoff = now_london() - timedelta(hours=max_age_hours)
    with event_lock:
        to_remove = []
        for sym, evt in pending_events.items():
            evt_time = evt.get('timestamp')
            if evt_time and to_london(evt_time) < cutoff:
                to_remove.append(sym)
        for sym in to_remove:
            del pending_events[sym]
            if sym in last_processed_event_time:
                del last_processed_event_time[sym]
        if to_remove:
            logger.info(f"{len(to_remove)} eski event temizlendi")

# ==================== TELEGRAM ENTegrasyonu ====================
def send_telegram(message, csv_path=None):
    token = CONFIG['TELEGRAM_BOT_TOKEN']
    chat_id = CONFIG['TELEGRAM_CHAT_ID']

    if token == 'YOUR_BOT_TOKEN' or chat_id == 'YOUR_CHAT_ID':
        logger.warning("Telegram token/chat_id ayarlanmamış")
        return False

    try:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        payload = {
            'chat_id': chat_id,
            'text': message,
            'parse_mode': 'HTML',
            'disable_web_page_preview': True
        }
        resp = requests.post(url, json=payload, timeout=15)
        if resp.status_code != 200:
            logger.error(f"Telegram mesaj hatası: {resp.text}")

        if csv_path and os.path.exists(csv_path):
            doc_url = f"https://api.telegram.org/bot{token}/sendDocument"
            with open(csv_path, 'rb') as f:
                files = {'document': f}
                data = {'chat_id': chat_id}
                resp = requests.post(doc_url, files=files, data=data, timeout=60)
                if resp.status_code == 200:
                    logger.info("Telegram'a CSV gönderildi")
                else:
                    logger.error(f"Telegram CSV hatası: {resp.text}")
        return True
    except Exception as e:
        logger.error(f"Telegram hatası: {e}")
        return False

# ==================== BACKTEST MOTORU (Londra Saati) ====================
class BacktestEngine:
    def __init__(self):
        self.results = []
        self.csv_path = CONFIG['CSV_OUTPUT']
        self.stats = {
            'total_scanned': 0,
            'phase_a_hits': 0,
            'phase_b_hits': 0,
            'errors': 0
        }

    def run(self):
        logger.info("="*60)
        logger.info("PUMP RADAR BACKTEST BAŞLATILIYOR")
        logger.info(f"Zaman dilimi: {LONDON_TZ}")
        logger.info(f"Şu anki Londra saati: {format_london(now_london())}")
        logger.info(f"Periyot: {CONFIG['BACKTEST_START']} - {CONFIG['BACKTEST_END']} (Londra)")
        logger.info("="*60)

        symbols = get_symbols()
        if not symbols:
            logger.error("Sembol listesi alınamadı!")
            return

        logger.info(f"{len(symbols)} sembol taranacak")

        start_dt = parse_london(CONFIG['BACKTEST_START'])
        end_dt = parse_london(CONFIG['BACKTEST_END'])

        logger.info(f"Başlangıç (Londra): {format_london(start_dt)}")
        logger.info(f"Bitiş (Londra): {format_london(end_dt)}")

        for idx, symbol in enumerate(symbols, 1):
            logger.info(f"[{idx}/{len(symbols)}] {symbol} backtest ediliyor...")
            self._backtest_symbol(symbol, start_dt, end_dt)

            if idx % 10 == 0:
                logger.info("Rate limit koruması - 2 saniye bekleniyor...")
                time.sleep(2)

        self._save_csv()
        self._print_stats()
        self._send_report()
        logger.info("BACKTEST TAMAMLANDI")

    def _backtest_symbol(self, symbol, start_dt, end_dt):
        current = start_dt
        symbol_signals = 0

        while current < end_dt and symbol_signals < CONFIG['MAX_BACKTEST_SIGNALS_PER_SYMBOL']:
            # Londra saatini UTC timestamp'e çevir (API UTC bekler)
            end_ts = int(current.astimezone(UTC_TZ).timestamp() * 1000)

            try:
                phase_a = phase_a_scan(symbol, end_ts)
                self.stats['total_scanned'] += 1

                if phase_a:
                    self.stats['phase_a_hits'] += 1

                    phase_b = phase_b_confirm(symbol, phase_a['timestamp'], end_ts)

                    if phase_b:
                        self.stats['phase_b_hits'] += 1
                        symbol_signals += 1

                        self.results.append({
                            'symbol': symbol,
                            'timestamp_london': format_london(phase_b['timestamp']),
                            'timestamp_utc': phase_b['timestamp'].astimezone(UTC_TZ).strftime('%Y-%m-%d %H:%M:%S UTC'),
                            'close': round(phase_b['close'], 6),
                            'score': phase_b['score'],
                            'support': round(phase_b['support'], 6) if phase_b['support'] else None,
                            'resistance': round(phase_b['resistance'], 6) if phase_b['resistance'] else None,
                            'volume': round(phase_b['volume'], 2),
                            'ema_break': phase_b['details'].get('ema_break', False),
                            'squeeze': phase_b['details'].get('squeeze', False),
                            'divergence': phase_b['details'].get('divergence', False),
                            'volume_ratio': phase_b['details'].get('volume_ratio', 0)
                        })
            except Exception as e:
                self.stats['errors'] += 1
                logger.error(f"Backtest hata {symbol} @ {format_london(current)}: {e}")

            current += timedelta(minutes=5)

    def _save_csv(self):
        if not self.results:
            logger.warning("Kaydedilecek sonuç bulunamadı")
            return

        df = pd.DataFrame(self.results)
        df.to_csv(self.csv_path, index=False, encoding='utf-8-sig')
        logger.info(f"CSV kaydedildi: {self.csv_path}")
        logger.info(f"Toplam sinyal: {len(self.results)}")

        if len(df) > 0:
            logger.info(f"Ortalama skor: {df['score'].mean():.1f}")
            logger.info(f"Max skor: {df['score'].max()}")
            logger.info(f"En çok sinyal: {df['symbol'].value_counts().head(3).to_dict()}")

    def _print_stats(self):
        logger.info("="*60)
        logger.info("BACKTEST İSTATİSTİKLERİ")
        logger.info(f"Zaman dilimi: {LONDON_TZ}")
        logger.info(f"Şu anki saat: {format_london(now_london())}")
        logger.info(f"Toplam tarama: {self.stats['total_scanned']}")
        logger.info(f"Faz A isabet: {self.stats['phase_a_hits']}")
        logger.info(f"Faz B isabet: {self.stats['phase_b_hits']}")
        logger.info(f"Hata sayısı: {self.stats['errors']}")
        if self.stats['phase_a_hits'] > 0:
            hit_rate = (self.stats['phase_b_hits'] / self.stats['phase_a_hits']) * 100
            logger.info(f"Faz B geçiş oranı: {hit_rate:.1f}%")
        logger.info("="*60)

    def _send_report(self):
        if not self.results:
            return

        df = pd.DataFrame(self.results)
        avg_score = df['score'].mean() if len(df) > 0 else 0

        msg = f"""<b>🎯 PUMP RADAR Backtest Tamamlandı</b>

📍 <b>Zaman Dilimi:</b> {LONDON_TZ}
⏰ <b>Bitiş Saati:</b> {format_london(now_london())}

📊 <b>İstatistikler:</b>
• Toplam Sinyal: {len(self.results)}
• Faz A İsabet: {self.stats['phase_a_hits']}
• Faz B İsabet: {self.stats['phase_b_hits']}
• Ortalama Skor: {avg_score:.1f}
• Hata Sayısı: {self.stats['errors']}

📅 Periyot: {CONFIG['BACKTEST_START']} - {CONFIG['BACKTEST_END']}
📁 CSV dosyası ektedir."""

        send_telegram(msg, self.csv_path)

# ==================== LIVE SCANNER (Londra Saati) ====================
class LiveScanner:
    def __init__(self):
        self.executor = ThreadPoolExecutor(max_workers=CONFIG['MAX_WORKERS'])
        self.scan_count = 0

    def run(self):
        logger.info("="*60)
        logger.info("PUMP RADAR LIVE SCANNER BAŞLATILIYOR")
        logger.info(f"Zaman dilimi: {LONDON_TZ}")
        logger.info(f"Şu anki Londra saati: {format_london(now_london())}")
        logger.info("Durdurmak için Ctrl+C")
        logger.info("="*60)

        try:
            while True:
                self._scan_cycle()
                self.scan_count += 1

                if self.scan_count % 1000 == 0:
                    cleanup_old_events()

                logger.info(f"Bekleniyor... {CONFIG['SCAN_INTERVAL']}sn | {format_london(now_london())}")
                time.sleep(CONFIG['SCAN_INTERVAL'])
        except KeyboardInterrupt:
            logger.info("Scanner durduruldu")
            self.executor.shutdown(wait=True)

    def _scan_cycle(self):
        symbols = get_symbols()
        if not symbols:
            logger.error("Sembol listesi boş!")
            return

        logger.info(f"{len(symbols)} sembol taranıyor... | {format_london(now_london())}")
        futures = {self.executor.submit(self._scan_symbol, sym): sym for sym in symbols}

        signals = []
        for future in as_completed(futures):
            symbol = futures[future]
            try:
                result = future.result()
                if result:
                    signals.append(result)
                    logger.info(f"🚨 SİNYAL: {symbol} | Skor: {result['score']} | {format_london(now_london())}")
            except Exception as e:
                logger.error(f"Scanner hata {symbol}: {e}")

        if signals:
            logger.info(f"Bu taramada {len(signals)} sinyal bulundu")

    def _scan_symbol(self, symbol):
        phase_a = phase_a_scan(symbol)
        if not phase_a:
            return None

        open_time = phase_a['open_time']

        if not check_new_event(symbol, open_time):
            return None

        phase_b = phase_b_confirm(symbol, open_time)
        if phase_b:
            set_pending_event(symbol, phase_b)

            msg = f"""<b>🚨 PUMP RADAR SİNYAL</b>

📍 <b>Zaman:</b> {format_london(phase_b['timestamp'])}
💎 Sembol: <code>{symbol}</code>
📈 Skor: <b>{phase_b['score']}/100</b>
💰 Fiyat: {phase_b['close']:.6f}
📊 Destek: {phase_b['support']:.6f if phase_b['support'] else 'N/A'}
🎯 Direnç: {phase_b['resistance']:.6f if phase_b['resistance'] else 'N/A'}
📦 Hacim: {phase_b['volume']:.2f}
🔍 Detaylar: {', '.join([k for k, v in phase_b['details'].items() if v and k != 'volume_ratio'])}
⏰ Tespit: {format_london(now_london())}"""

            send_telegram(msg)
            return phase_b

        return None

# ==================== MAIN ====================
if __name__ == '__main__':
    print(f"""
    ╔══════════════════════════════════════════════════╗
    ║          PUMP RADAR v2.1 - Londra Saati          ║
    ║  Zaman Dilimi: {LONDON_TZ:<36}║
    ║  Şu An: {format_london(now_london()):<45}║
    ╚══════════════════════════════════════════════════╝
    """)

    if CONFIG['BACKTEST_MODE']:
        engine = BacktestEngine()
        engine.run()
    else:
        scanner = LiveScanner()
        scanner.run()
