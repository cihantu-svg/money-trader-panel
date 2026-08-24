#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PUMP PULLBACK RADAR v1.0
Strateji:
- 1h mumda %10+ pump tespiti
- 5m'de S/R pullback (causal pivot, 50 mum)
- Hacim filtresi: 3M USD
- Backtest: %3 SL / %8 TP
- CSV + Telegram
"""

import threading
import time
import os
import sys
import logging
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
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
    'MAX_COINS': 150,
    'MIN_VOLUME_USD': 3_000_000,      # 3M USD hacim filtresi
    'PUMP_PCT': 10.0,                  # 1h'da %10+ pump
    'PULLBACK_WINDOW': 50,            # 5m'de son 50 mum
    'SL_PCT': 3.0,                     # %3 stop loss
    'TP_PCT': 8.0,                     # %8 take profit
    'MAX_WORKERS': 8,
    'TELEGRAM_BOT_TOKEN': os.getenv('TELEGRAM_BOT_TOKEN', 'YOUR_BOT_TOKEN'),
    'TELEGRAM_CHAT_ID': os.getenv('TELEGRAM_CHAT_ID', 'YOUR_CHAT_ID'),
    'BACKTEST_MODE': True,
    'BACKTEST_START': '2024-01-01',
    'BACKTEST_END': '2024-01-15',     # 2 haftalık test
    'CSV_OUTPUT': 'pump_pullback_results.csv',
    'SYMBOL_CACHE_TTL': 3600,
}

# ==================== GLOBALS ====================
_lock = threading.Lock()
_sym_cache = None
_sym_cache_t = 0

# ==================== API ====================
class API:
    def __init__(self):
        self.s = requests.Session()
        self.s.headers.update({'User-Agent': 'PUMP-PB/1.0'})
        self.base = CFG['API_BASE']
        self.last = 0
        self.min_int = 0.05

    def _rl(self):
        n = time.time()
        if n - self.last < self.min_int:
            time.sleep(self.min_int - (n - self.last))
        self.last = time.time()

    def get(self, endpoint, params=None):
        self._rl()
        try:
            r = self.s.get(f"{self.base}{endpoint}", params=params, timeout=15)
            r.raise_for_status()
            return r.json()
        except Exception as e:
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

# ==================== SYMBOLS ====================
def get_symbols():
    global _sym_cache, _sym_cache_t
    with _lock:
        if _sym_cache and (time.time() - _sym_cache_t) < CFG['SYMBOL_CACHE_TTL']:
            return _sym_cache

    info = api.get('/fapi/v1/exchangeInfo')
    if not info:
        return []

    syms = [s['symbol'] for s in info.get('symbols', [])
            if s.get('status') == 'TRADING' and s.get('contractType') == 'PERPETUAL' and s['symbol'].endswith('USDT')]
    syms = sorted(syms)[:CFG['MAX_COINS']]

    with _lock:
        _sym_cache = syms
        _sym_cache_t = time.time()
    log.info(f"{len(syms)} sembol yüklendi")
    return syms

# ==================== TOPLU VERİ ÇEKME ====================
def fetch_all(symbol, interval, start_dt, end_dt, limit=1500):
    """Belirli aralık için TÜM klines verisini chunk'lar halinde çeker."""
    chunks = []
    cur = start_dt
    end_ts = int(end_dt.astimezone(UTC).timestamp() * 1000)

    while cur < end_dt:
        chunk_end = int(cur.astimezone(UTC).timestamp() * 1000) + (limit * 60 * 1000)
        chunk_end = min(chunk_end, end_ts)

        df = api.klines(symbol, interval, limit, endTime=chunk_end)
        if df is None or len(df) == 0:
            break

        chunks.append(df)
        last_close = df['close_time'].iloc[-1]
        if last_close >= end_dt or len(df) < limit:
            break
        cur = last_close + timedelta(minutes=1)
        time.sleep(0.05)

    if not chunks:
        return None

    combined = pd.concat(chunks, ignore_index=True)
    combined = combined.drop_duplicates(subset=['open_time']).sort_values('open_time').reset_index(drop=True)
    combined = combined[(combined['open_time'] >= start_dt) & (combined['open_time'] <= end_dt)]
    return combined.reset_index(drop=True)

# ==================== S/R (Causal Pivot) ====================
def compute_sr(df, left=5, right=5):
    """Repaint-siz causal pivot S/R."""
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
def find_pumps(df_1h, min_pct=10.0):
    """
    1h DataFrame'de %10+ pump olan mumların indexlerini döndür.
    Mum yüzdesi: (close - open) / open * 100
    """
    if df_1h is None or len(df_1h) < 2:
        return []
    df = df_1h.copy()
    df['pct'] = (df['close'] - df['open']) / df['open'] * 100
    df['volume_usd'] = df['quote_volume']
    pumps = df[(df['pct'] >= min_pct) & (df['volume_usd'] >= CFG['MIN_VOLUME_USD'])]
    return pumps.index.tolist()

# ==================== PULLBACK SİNYALİ ====================
def find_pullback(df_5m, entry_price, window=50):
    """
    Son `window` mum içinde fiyat S/R desteğine dokunup geri döndü mü?
    Dönüş: (sinyal_mumu_index, support_seviyesi) veya None
    """
    if df_5m is None or len(df_5m) < window:
        return None

    # Son window mum
    w = df_5m.iloc[-window:].copy().reset_index(drop=True)
    sup, res = compute_sr(w)

    if sup is None:
        return None

    # Fiyat desteğe dokundu mu ve geri döndü mü?
    # Kriter: Low <= support VE Close > support (dönüş mumu)
    for i in range(len(w)):
        if w['low'].iloc[i] <= sup * 1.005 and w['close'].iloc[i] > sup:
            return i, sup

    return None

# ==================== POZİSYON YÖNETİMİ ====================
def simulate_trade(entry_price, df_5m_after, entry_idx):
    """
    Entry'den sonraki 5m mumlarında %3 SL / %8 TP takip et.
    Dönüş: sl_hit, tp_hit, exit_price, exit_time, max_dd, max_profit
    """
    if entry_idx >= len(df_5m_after):
        return False, False, entry_price, None, 0, 0

    sl_price = entry_price * (1 - CFG['SL_PCT'] / 100)
    tp_price = entry_price * (1 + CFG['TP_PCT'] / 100)

    sl_hit = False
    tp_hit = False
    exit_price = entry_price
    exit_time = None
    max_dd = 0
    max_profit = 0

    for i in range(entry_idx, len(df_5m_after)):
        low = df_5m_after['low'].iloc[i]
        high = df_5m_after['high'].iloc[i]
        close = df_5m_after['close'].iloc[i]
        ts = df_5m_after['close_time'].iloc[i]

        dd = (low - entry_price) / entry_price * 100
        profit = (high - entry_price) / entry_price * 100
        max_dd = min(max_dd, dd)
        max_profit = max(max_profit, profit)

        if low <= sl_price:
            sl_hit = True
            exit_price = sl_price
            exit_time = ts
            break
        if high >= tp_price:
            tp_hit = True
            exit_price = tp_price
            exit_time = ts
            break

    if not sl_hit and not tp_hit:
        exit_price = df_5m_after['close'].iloc[-1]
        exit_time = df_5m_after['close_time'].iloc[-1]

    return sl_hit, tp_hit, exit_price, exit_time, max_dd, max_profit

# ==================== TELEGRAM ====================
def send_tg(msg, csv_path=None):
    tok = CFG['TELEGRAM_BOT_TOKEN']
    cid = CFG['TELEGRAM_CHAT_ID']
    if tok == 'YOUR_BOT_TOKEN' or cid == 'YOUR_CHAT_ID':
        log.warning("Telegram ayarlanmamış")
        return False

    try:
        r = requests.post(f"https://api.telegram.org/bot{tok}/sendMessage",
                          json={'chat_id': cid, 'text': msg, 'parse_mode': 'HTML'}, timeout=15)
        if r.status_code != 200:
            log.error(f"TG msg: {r.text}")

        if csv_path and os.path.exists(csv_path):
            with open(csv_path, 'rb') as f:
                r2 = requests.post(f"https://api.telegram.org/bot{tok}/sendDocument",
                                   files={'document': f}, data={'chat_id': cid}, timeout=60)
                if r2.status_code == 200:
                    log.info("CSV Telegram'a gönderildi")
        return True
    except Exception as e:
        log.error(f"TG hata: {e}")
        return False

# ==================== BACKTEST ====================
class Backtest:
    def __init__(self):
        self.results = []
        self.csv = CFG['CSV_OUTPUT']
        self.stats = {'scanned': 0, 'pump_found': 0, 'signal': 0, 'sl': 0, 'tp': 0, 'errors': 0}

    def run(self):
        log.info("="*60)
        log.info("PUMP PULLBACK BACKTEST")
        log.info(f"Zaman: {fmt(now())}")
        log.info(f"Periyot: {CFG['BACKTEST_START']} - {CFG['BACKTEST_END']}")
        log.info(f"Pump: %{CFG['PUMP_PCT']}+ | SL: %{CFG['SL_PCT']} | TP: %{CFG['TP_PCT']}")
        log.info("="*60)

        syms = get_symbols()
        if not syms:
            log.error("Sembol yok")
            return

        start = datetime.strptime(CFG['BACKTEST_START'], '%Y-%m-%d').replace(tzinfo=LONDON)
        end = datetime.strptime(CFG['BACKTEST_END'], '%Y-%m-%d').replace(tzinfo=LONDON)

        for idx, sym in enumerate(syms, 1):
            log.info(f"[{idx}/{len(syms)}] {sym} çekiliyor...")
            self._test_symbol(sym, start, end)

        self._save()
        self._report()
        send_tg(self._summary_msg(), self.csv)
        log.info("BACKTEST BİTTİ")

    def _test_symbol(self, sym, start, end):
        try:
            # 1h verisi çek
            df1h = fetch_all(sym, '1h', start, end)
            if df1h is None or len(df1h) < 2:
                return

            # 5m verisi çek
            df5m = fetch_all(sym, '5m', start, end)
            if df5m is None or len(df5m) < 50:
                return

            # Pump tespiti
            pump_indices = find_pumps(df1h)
            self.stats['scanned'] += len(df1h)

            if not pump_indices:
                return

            self.stats['pump_found'] += len(pump_indices)
            log.info(f"  {sym}: {len(pump_indices)} pump bulundu")

            for pidx in pump_indices:
                pump_time = df1h['close_time'].iloc[pidx]
                pump_open = df1h['open'].iloc[pidx]
                pump_close = df1h['close'].iloc[pidx]
                pump_pct = (pump_close - pump_open) / pump_open * 100
                pump_vol = df1h['quote_volume'].iloc[pidx]

                # Pump'tan sonraki 5m verisi
                after_pump = df5m[df5m['open_time'] > pump_time].copy().reset_index(drop=True)
                if len(after_pump) < 10:
                    continue

                # Pullback ara (pump'tan sonraki ilk 50 5m mum)
                pb = find_pullback(after_pump, pump_close, CFG['PULLBACK_WINDOW'])
                if pb is None:
                    continue

                pb_idx, sup_level = pb
                entry_price = after_pump['close'].iloc[pb_idx]
                entry_time = after_pump['close_time'].iloc[pb_idx]

                # Pozisyon simülasyonu
                sl_hit, tp_hit, exit_p, exit_t, max_dd, max_prof = simulate_trade(
                    entry_price, after_pump, pb_idx + 1
                )

                result_pct = (exit_p - entry_price) / entry_price * 100

                self.stats['signal'] += 1
                if sl_hit:
                    self.stats['sl'] += 1
                if tp_hit:
                    self.stats['tp'] += 1

                self.results.append({
                    'symbol': sym,
                    'pump_time': fmt(pump_time),
                    'pump_pct': round(pump_pct, 2),
                    'pump_volume_usd': round(pump_vol, 2),
                    'entry_time': fmt(entry_time),
                    'entry_price': round(entry_price, 6),
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
            self.stats['errors'] += 1
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
        log.info(f"Pump bulunan: {self.stats['pump_found']}")
        log.info(f"Sinyal: {self.stats['signal']}")
        log.info(f"SL: {self.stats['sl']} | TP: {self.stats['tp']}")
        log.info(f"Hata: {self.stats['errors']}")
        if self.stats['signal'] > 0:
            tp_rate = self.stats['tp'] / self.stats['signal'] * 100
            log.info(f"TP Oranı: {tp_rate:.1f}%")
        log.info("="*60)

    def _summary_msg(self):
        if not self.results:
            return "<b>PUMP PULLBACK</b>\nSonuç bulunamadı."
        df = pd.DataFrame(self.results)
        win = self.stats['tp']
        loss = self.stats['sl']
        total = self.stats['signal']
        tp_rate = win / total * 100 if total else 0
        avg_res = df['result_pct'].mean()
        return f"""<b>🎯 PUMP PULLBACK Backtest</b>

📊 Sonuçlar:
• Sinyal: {total}
• TP: {win} | SL: {loss}
• TP Oranı: {tp_rate:.1f}%
• Ort. Getiri: {avg_res:.2f}%

📁 CSV ekte."""

# ==================== MAIN ====================
if __name__ == '__main__':
    print(f"""
    ╔══════════════════════════════════════════════╗
    ║     PUMP PULLBACK RADAR v1.0                ║
    ║     {fmt(now())}                  ║
    ╚══════════════════════════════════════════════╝
    """)
    Backtest().run()
