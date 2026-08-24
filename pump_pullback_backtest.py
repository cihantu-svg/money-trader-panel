#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PUMP PULLBACK RADAR v1.2 - 300 Coin / 3 Hafta / Rate Limit Güvenli
Strateji:
- 1h mumda %10+ pump tespiti (3M USD hacim)
- 5m'de S/R pullback (causal pivot, 50 mum)
- Backtest: %3 SL / %8 TP
- CSV + Telegram
"""

import time
import os
import sys
import logging
from datetime import datetime, timedelta, timezone
import requests
import numpy as np
import pandas as pd

try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo

# ==================== ZAMAN ====================
LONDON = ZoneInfo("Europe/London")
UTC = timezone.utc

def now():
    return datetime.now(LONDON)

def fmt(dt):
    if dt is None:
        return 'N/A'
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(LONDON).strftime('%Y-%m-%d %H:%M:%S %Z')

# ==================== LOGGING ====================
class Fmt(logging.Formatter):
    def formatTime(self, record, datefmt=None):
        return datetime.fromtimestamp(record.created, LONDON).strftime('%Y-%m-%d %H:%M:%S %Z')

h = logging.StreamHandler(sys.stdout)
h.setFormatter(Fmt('%(asctime)s | %(levelname)s | %(message)s'))
fh = logging.FileHandler('pump_pullback.log', encoding='utf-8')
fh.setFormatter(Fmt('%(asctime)s | %(levelname)s | %(message)s'))
logging.basicConfig(level=logging.INFO, handlers=[h, fh])
log = logging.getLogger('PUMP_PB')

# ==================== CONFIG ====================
CFG = {
    'API_BASE': 'https://fapi.binance.com',
    'MAX_COINS': 300,
    'MIN_VOLUME_USD': 3_000_000,
    'PUMP_PCT': 10.0,
    'PULLBACK_WINDOW': 50,
    'SL_PCT': 3.0,
    'TP_PCT': 8.0,
    'TELEGRAM_BOT_TOKEN': os.getenv('TELEGRAM_BOT_TOKEN', 'YOUR_BOT_TOKEN'),
    'TELEGRAM_CHAT_ID': os.getenv('TELEGRAM_CHAT_ID', 'YOUR_CHAT_ID'),
    'BACKTEST_MODE': True,
    'BACKTEST_START': '2024-01-01',
    'BACKTEST_END': '2024-01-22',     # 3 hafta
    'CSV_OUTPUT': 'pump_pullback_results.csv',
    'SYMBOL_CACHE_TTL': 3600,
    'API_DELAY': 0.2,                # 5 req/s = güvenli
    'RETRY_MAX': 5,
    'RETRY_BASE': 10,                # 10-20-40-80-160sn
}

# ==================== API (Rate Limit + Retry) ====================
class API:
    def __init__(self):
        self.s = requests.Session()
        self.s.headers.update({'User-Agent': 'PUMP-PB/1.2'})
        self.base = CFG['API_BASE']
        self.last = 0
        self.delay = CFG['API_DELAY']
        self.req_count = 0

    def _rl(self):
        n = time.time()
        wait = self.delay - (n - self.last)
        if wait > 0:
            time.sleep(wait)
        self.last = time.time()
        self.req_count += 1

    def get(self, endpoint, params=None, retries=0):
        self._rl()
        try:
            r = self.s.get(f"{self.base}{endpoint}", params=params, timeout=20)

            if r.status_code == 429:
                retry_after = int(r.headers.get('Retry-After', CFG['RETRY_BASE'] * (2 ** retries)))
                if retries < CFG['RETRY_MAX']:
                    log.warning(f"429 → {retry_after}sn bekleniyor... ({retries+1}/{CFG['RETRY_MAX']})")
                    time.sleep(retry_after)
                    return self.get(endpoint, params, retries + 1)
                else:
                    log.error(f"429 - Max retry aşıldı")
                    return None

            if r.status_code == 418:
                log.error("IP BAN (418) - Çok fazla istek")
                return None

            r.raise_for_status()
            return r.json()
        except requests.exceptions.RequestException as e:
            log.error(f"API {endpoint}: {e}")
            return None

    def klines(self, symbol, interval, limit, endTime=None):
        p = {'symbol': symbol, 'interval': interval, 'limit': limit}
        if endTime:
            p['endTime'] = int(endTime)
        d = self.get('/fapi/v1/klines', p)
        if not d or not isinstance(d, list):
            return None
        df = pd.DataFrame(d, columns=[
            'open_time','open','high','low','close','volume',
            'close_time','quote_volume','trades','taker_buy_base',
            'taker_buy_quote','ignore'
        ])
        df['open_time'] = pd.to_datetime(df['open_time'], unit='ms', utc=True).dt.tz_convert(LONDON)
        df['close_time'] = pd.to_datetime(df['close_time'], unit='ms', utc=True).dt.tz_convert(LONDON)
        for c in ['open','high','low','close','volume','quote_volume']:
            df[c] = pd.to_numeric(df[c], errors='coerce')
        return df.dropna()

api = API()

# ==================== SYMBOLS (Cache) ====================
_sym_cache = None
_sym_cache_t = 0

def get_symbols():
    global _sym_cache, _sym_cache_t
    if _sym_cache and (time.time() - _sym_cache_t) < CFG['SYMBOL_CACHE_TTL']:
        return _sym_cache

    info = api.get('/fapi/v1/exchangeInfo')
    if not info:
        return []

    syms = [s['symbol'] for s in info.get('symbols', [])
            if s.get('status') == 'TRADING' and s.get('contractType') == 'PERPETUAL' and s['symbol'].endswith('USDT')]
    syms = sorted(syms)[:CFG['MAX_COINS']]
    _sym_cache = syms
    _sym_cache_t = time.time()
    log.info(f"{len(syms)} sembol yüklendi")
    return syms

# ==================== TOPLU VERİ ÇEKME ====================
def fetch_all(symbol, interval, start_dt, end_dt, limit=1500):
    """Tüm veriyi chunk'lar halinde çeker. Sıralı, thread yok."""
    chunks = []
    end_ts = int(end_dt.astimezone(UTC).timestamp() * 1000)

    # Kaç chunk gerek? (Binance max 1500 mum)
    if interval == '1h':
        mins_per_candle = 60
    elif interval == '5m':
        mins_per_candle = 5
    else:
        mins_per_candle = 1

    total_minutes = (end_dt - start_dt).total_seconds() / 60
    total_candles = int(total_minutes / mins_per_candle)
    expected_chunks = max(1, (total_candles + limit - 1) // limit)

    cur_end = end_ts
    chunk_idx = 0

    while chunk_idx < expected_chunks * 2:  # güvenlik limiti
        chunk_idx += 1
        df = api.klines(symbol, interval, limit, endTime=cur_end)

        if df is None or len(df) == 0:
            break

        chunks.append(df)
        first_time = df['open_time'].iloc[0]

        # Başlangıca ulaştık mı?
        if first_time <= start_dt or len(df) < limit:
            break

        # Bir sonraki chunk
        cur_end = int((first_time - timedelta(minutes=1)).astimezone(UTC).timestamp() * 1000)

    if not chunks:
        return None

    combined = pd.concat(chunks, ignore_index=True)
    combined = combined.drop_duplicates(subset=['open_time']).sort_values('open_time').reset_index(drop=True)
    combined = combined[(combined['open_time'] >= start_dt) & (combined['open_time'] <= end_dt)]
    return combined.reset_index(drop=True)

# ==================== S/R ====================
def compute_sr(df, left=5, right=5):
    h = df['high'].values
    l = df['low'].values
    n = len(df)
    ph = np.zeros(n, dtype=bool)
    pl = np.zeros(n, dtype=bool)

    for i in range(left, n - right):
        if h[i] >= np.max(h[i-left:i]) and h[i] >= np.max(h[i+1:i+right+1]):
            ph[i] = True
        if l[i] <= np.min(l[i-left:i]) and l[i] <= np.min(l[i+1:i+right+1]):
            pl[i] = True

    res = df.loc[ph, 'high'].tail(3).values
    sup = df.loc[pl, 'low'].tail(3).values
    return (float(sup[-1]) if len(sup) else None,
            float(res[-1]) if len(res) else None)

# ==================== PUMP TESPİTİ ====================
def find_pumps(df_1h):
    if df_1h is None or len(df_1h) < 2:
        return []
    df = df_1h.copy()
    df['pct'] = (df['close'] - df['open']) / df['open'] * 100
    pumps = df[(df['pct'] >= CFG['PUMP_PCT']) & (df['quote_volume'] >= CFG['MIN_VOLUME_USD'])]
    return pumps.index.tolist()

# ==================== PULLBACK ====================
def find_pullback(df_5m, window=50):
    if df_5m is None or len(df_5m) < window:
        return None
    w = df_5m.iloc[-window:].copy().reset_index(drop=True)
    sup, _ = compute_sr(w)
    if sup is None:
        return None
    for i in range(len(w)):
        if w['low'].iloc[i] <= sup * 1.005 and w['close'].iloc[i] > sup:
            return i, sup, w['close'].iloc[i], w['close_time'].iloc[i]
    return None

# ==================== POZİSYON ====================
def simulate(entry_price, df_after, entry_idx):
    if entry_idx >= len(df_after):
        return False, False, entry_price, None, 0, 0

    sl = entry_price * (1 - CFG['SL_PCT'] / 100)
    tp = entry_price * (1 + CFG['TP_PCT'] / 100)
    sl_hit = tp_hit = False
    exit_p = entry_price
    exit_t = None
    max_dd = max_prof = 0

    for i in range(entry_idx, len(df_after)):
        low = df_after['low'].iloc[i]
        high = df_after['high'].iloc[i]
        ts = df_after['close_time'].iloc[i]

        dd = (low - entry_price) / entry_price * 100
        prof = (high - entry_price) / entry_price * 100
        max_dd = min(max_dd, dd)
        max_prof = max(max_prof, prof)

        if low <= sl:
            sl_hit = True
            exit_p = sl
            exit_t = ts
            break
        if high >= tp:
            tp_hit = True
            exit_p = tp
            exit_t = ts
            break

    if not sl_hit and not tp_hit:
        exit_p = df_after['close'].iloc[-1]
        exit_t = df_after['close_time'].iloc[-1]

    return sl_hit, tp_hit, exit_p, exit_t, max_dd, max_prof

# ==================== TELEGRAM ====================
def send_tg(msg, csv_path=None):
    tok, cid = CFG['TELEGRAM_BOT_TOKEN'], CFG['TELEGRAM_CHAT_ID']
    if tok == 'YOUR_BOT_TOKEN' or cid == 'YOUR_CHAT_ID':
        return False
    try:
        r = requests.post(f"https://api.telegram.org/bot{tok}/sendMessage",
                          json={'chat_id': cid, 'text': msg, 'parse_mode': 'HTML'}, timeout=15)
        if csv_path and os.path.exists(csv_path):
            with open(csv_path, 'rb') as f:
                requests.post(f"https://api.telegram.org/bot{tok}/sendDocument",
                             files={'document': f}, data={'chat_id': cid}, timeout=60)
        return True
    except Exception as e:
        log.error(f"TG: {e}")
        return False

# ==================== BACKTEST ====================
class Backtest:
    def __init__(self):
        self.results = []
        self.csv = CFG['CSV_OUTPUT']
        self.stats = {'scanned': 0, 'pump': 0, 'signal': 0, 'sl': 0, 'tp': 0, 'err': 0}

    def run(self):
        log.info("="*60)
        log.info("PUMP PULLBACK BACKTEST")
        log.info(f"Zaman: {fmt(now())}")
        log.info(f"Periyot: {CFG['BACKTEST_START']} - {CFG['BACKTEST_END']} (3 hafta)")
        log.info(f"Coin: {CFG['MAX_COINS']} | Pump: %{CFG['PUMP_PCT']}+ | SL/TP: %{CFG['SL_PCT']}/%{CFG['TP_PCT']}")
        log.info("="*60)

        syms = get_symbols()
        if not syms:
            log.error("Sembol yok")
            return

        start = datetime.strptime(CFG['BACKTEST_START'], '%Y-%m-%d').replace(tzinfo=LONDON)
        end = datetime.strptime(CFG['BACKTEST_END'], '%Y-%m-%d').replace(tzinfo=LONDON)

        total_api_calls = len(syms) * 6  # ~1h(1) + 5m(5)
        est_time = total_api_calls * CFG['API_DELAY'] / 60
        log.info(f"Tahmini API çağrısı: ~{total_api_calls} | Süre: ~{est_time:.0f} dk")
        log.info("="*60)

        for idx, sym in enumerate(syms, 1):
            if idx % 10 == 1:
                log.info(f"[{idx}/{len(syms)}] {sym} çekiliyor...")
            self._test(sym, start, end)

        self._save()
        self._report()
        send_tg(self._summary(), self.csv)
        log.info("BACKTEST BİTTİ")

    def _test(self, sym, start, end):
        try:
            df1h = fetch_all(sym, '1h', start, end)
            if df1h is None or len(df1h) < 2:
                return

            df5m = fetch_all(sym, '5m', start, end)
            if df5m is None or len(df5m) < 50:
                return

            self.stats['scanned'] += len(df1h)
            pumps = find_pumps(df1h)

            if not pumps:
                return

            self.stats['pump'] += len(pumps)

            for pidx in pumps:
                p_time = df1h['close_time'].iloc[pidx]
                p_open = df1h['open'].iloc[pidx]
                p_close = df1h['close'].iloc[pidx]
                p_pct = (p_close - p_open) / p_open * 100
                p_vol = df1h['quote_volume'].iloc[pidx]

                after = df5m[df5m['open_time'] > p_time].copy().reset_index(drop=True)
                if len(after) < 10:
                    continue

                pb = find_pullback(after, CFG['PULLBACK_WINDOW'])
                if pb is None:
                    continue

                _, sup_level, entry_p, entry_t = pb
                sl_hit, tp_hit, exit_p, exit_t, max_dd, max_prof = simulate(entry_p, after, pb[0] + 1)
                result_pct = (exit_p - entry_p) / entry_p * 100

                self.stats['signal'] += 1
                if sl_hit:
                    self.stats['sl'] += 1
                if tp_hit:
                    self.stats['tp'] += 1

                self.results.append({
                    'symbol': sym,
                    'pump_time': fmt(p_time),
                    'pump_pct': round(p_pct, 2),
                    'pump_volume_usd': round(p_vol, 2),
                    'entry_time': fmt(entry_t),
                    'entry_price': round(entry_p, 6),
                    'support': round(sup_level, 6),
                    'sl_hit': sl_hit,
                    'tp_hit': tp_hit,
                    'exit_price': round(exit_p, 6),
                    'exit_time': fmt(exit_t),
                    'result_pct': round(result_pct, 2),
                    'max_dd_pct': round(max_dd, 2),
                    'max_profit_pct': round(max_prof, 2),
                })

        except Exception as e:
            self.stats['err'] += 1
            log.error(f"Hata {sym}: {e}")

    def _save(self):
        if not self.results:
            log.warning("Sonuç yok")
            return
        df = pd.DataFrame(self.results)
        df.to_csv(self.csv, index=False, encoding='utf-8-sig')
        log.info(f"CSV: {self.csv} | {len(self.results)} sinyal")

    def _report(self):
        log.info("="*60)
        log.info("SONUÇLAR")
        log.info(f"Taranan 1h mum: {self.stats['scanned']}")
        log.info(f"Pump: {self.stats['pump']} | Sinyal: {self.stats['signal']}")
        log.info(f"SL: {self.stats['sl']} | TP: {self.stats['tp']}")
        log.info(f"Hata: {self.stats['err']} | API çağrısı: {api.req_count}")
        if self.stats['signal'] > 0:
            log.info(f"TP Oranı: {self.stats['tp']/self.stats['signal']*100:.1f}%")
        log.info("="*60)

    def _summary(self):
        if not self.results:
            return "<b>PUMP PULLBACK</b>\nSonuç yok."
        df = pd.DataFrame(self.results)
        return f"""<b>🎯 PUMP PULLBACK Backtest</b>

📊 Sonuçlar:
• Sinyal: {self.stats['signal']}
• TP: {self.stats['tp']} | SL: {self.stats['sl']}
• TP Oranı: {self.stats['tp']/self.stats['signal']*100:.1f}%
• Ort. Getiri: {df['result_pct'].mean():.2f}%
• API Çağrısı: {api.req_count}

📁 CSV ekte."""

# ==================== MAIN ====================
if __name__ == '__main__':
    print(f"""
    ╔══════════════════════════════════════════════╗
    ║     PUMP PULLBACK RADAR v1.2                ║
    ║     {fmt(now())}                  ║
    ╚══════════════════════════════════════════════╝
    """)
    Backtest().run()
