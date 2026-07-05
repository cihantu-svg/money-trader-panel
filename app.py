# -*- coding: utf-8 -*-
"""
MONEY TRADER - BINANCE FUTURES SİNYAL PANELİ (STREAMLIT WEB SÜRÜMÜ)
✅ Pine Script ile BİREBİR UYUMLU RSI/Fibo Onayı
✅ SADECE SON MUMDA GELEN SİNYALLER (REPAINT YOK!)
✅ ÇOKLU ZAMAN DİLİMİ: 5dk, 15dk, 30dk, 1sa, 4sa, 1gün
✅ TÜM Binance Futures (USDT-M Perpetual) paritelerini tarar
✅ Tarayıcıdan / telefondan erişilebilir (orijinal tkinter masaüstü sürümünün web hali)
"""
import os
import json
import time
import hashlib
from datetime import datetime

import numpy as np
import pandas as pd
import requests
import streamlit as st
import urllib3
import warnings

urllib3.disable_warnings()
warnings.filterwarnings('ignore')

# ══════════════════════════════════════════════════════════════
# 🔧 VARSAYILAN AYARLAR
# ══════════════════════════════════════════════════════════════
DEFAULT_SETTINGS = {
    "enable_tavan_al": True,
    "enable_sat": True,
    "enable_quantum_al": True,
    "enable_obv_al": True,
    "enable_obv_sat": True,

    "rsi_period": 14,
    "ema_period": 20,
    "mom_period": 9,
    "vol_ma_period": 20,
    "fib_len": 14,

    "obv_ema_len": 3,

    "tavan_breakout_pct": 0.5,
    "tavan_min_vol_ratio": 1.2,

    "sat_breakout_pct": 0.5,
    "sat_min_vol_ratio": 1.2,

    "zone_lookback": 500,
    "auto_zone_days": 50,
    "auto_peak_pct": 3.0,
    "auto_dip_pct": 3.0,
    "auto_major_bins": 10,

    "hassasiyet": 0.1,
    "atr_period": 14,
    "atr_mult": 2.0,
    "base_olasilik": 65.0,
    "guc_carpan": 50.0,
    "olasilik_artis_bolucu": 4.0,
    "ek_olasilik_bonusu": 12.0,
    "hassasiyet_carpan": 2.5,
    "max_olasilik": 98.0,
    "min_guc_seviyesi": 0.0,

    "default_timeframe": "1h",
}

TIMEFRAME_OPTIONS = {
    "5m": {"label": "5 Dakika", "interval": "5m", "limit": 1000},
    "15m": {"label": "15 Dakika", "interval": "15m", "limit": 1000},
    "30m": {"label": "30 Dakika", "interval": "30m", "limit": 1000},
    "1h": {"label": "1 Saat", "interval": "1h", "limit": 1000},
    "4h": {"label": "4 Saat", "interval": "4h", "limit": 500},
    "1d": {"label": "Günlük", "interval": "1d", "limit": 500},
}

TRADES_FILE = "active_trades_binance_futures.json"

# ══════════════════════════════════════════════════════════════
# 📊 TEKNİK GÖSTERGE HESAPLAMALARI (orijinal mantık korunmuştur)
# ══════════════════════════════════════════════════════════════
def calc_rsi(series, period=14):
    """Wilder's RSI - Pine Script'in ta.rsi() ile birebir aynı"""
    delta = series.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = -delta.where(delta < 0, 0.0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    return rsi.fillna(50)


def calc_ema(series, period):
    return series.ewm(span=period, adjust=False).mean()


def calc_sma(series, period):
    return series.rolling(window=period).mean()


def calc_atr(df, period=14):
    high, low, close = df['High'], df['Low'], df['Close']
    tr1 = high - low
    tr2 = abs(high - close.shift())
    tr3 = abs(low - close.shift())
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    return tr.rolling(window=period).mean()


def calc_momentum(series, period=9):
    return series.diff(period)


def calc_heikin_ashi(df):
    ha_close = (df['Open'] + df['High'] + df['Low'] + df['Close']) / 4
    ha_open = pd.Series(index=df.index, dtype=float)
    ha_open.iloc[0] = (df['Open'].iloc[0] + df['Close'].iloc[0]) / 2
    for i in range(1, len(df)):
        ha_open.iloc[i] = (ha_open.iloc[i - 1] + ha_close.iloc[i - 1]) / 2
    ha_high = pd.concat([df['High'], ha_open, ha_close], axis=1).max(axis=1)
    ha_low = pd.concat([df['Low'], ha_open, ha_close], axis=1).min(axis=1)
    return pd.DataFrame({'haOpen': ha_open, 'haHigh': ha_high, 'haLow': ha_low, 'haClose': ha_close}, index=df.index)


def calc_obv(df):
    obv = [0]
    for i in range(1, len(df)):
        if df['Close'].iloc[i] > df['Close'].iloc[i - 1]:
            obv.append(obv[-1] + df['Volume'].iloc[i])
        elif df['Close'].iloc[i] < df['Close'].iloc[i - 1]:
            obv.append(obv[-1] - df['Volume'].iloc[i])
        else:
            obv.append(obv[-1])
    return pd.Series(obv, index=df.index)


def calc_vol_ratio(df, period=20):
    vol_sma = df['Volume'].rolling(window=period).mean()
    return df['Volume'] / vol_sma.replace(0, np.nan)


# ══════════════════════════════════════════════════════════════
# 🌐 BINANCE FUTURES API (USDT-M Perpetual)
# ⚠️ fapi.binance.com için Spot'taki gibi coğrafi kısıtlamasız resmi bir ayna
#    adres yoktur. Streamlit Cloud (ABD sunucusu) üzerinden 451 hatası alma
#    ihtimali vardır. Alırsanız bana bildirin, alternatif barındırma öneririm.
# ══════════════════════════════════════════════════════════════
BINANCE_BASE = "https://fapi.binance.com"
BINANCE_SESSION = requests.Session()
BINANCE_SESSION.headers.update({"User-Agent": "MoneyTrader-BinanceFutures/1.0"})


def get_binance_usdt_symbols(min_volume=0):
    try:
        r = BINANCE_SESSION.get(f"{BINANCE_BASE}/fapi/v1/ticker/24hr", timeout=30, verify=False)
        if r.status_code != 200:
            detail = f"HTTP {r.status_code}"
            try:
                detail += f" — {r.text[:300]}"
            except Exception:
                pass
            st.session_state.setdefault("errors", []).append(f"Binance Futures API hatasi: {detail}")
            return []
        data = r.json()
        if not isinstance(data, list):
            st.session_state.setdefault("errors", []).append(
                f"Binance Futures beklenmeyen yanit formati: {str(data)[:300]}"
            )
            return []

        exclude = ("3L", "3S", "5L", "5S", "UP", "DOWN", "BULL", "BEAR")
        symbols = []
        for t in data:
            sym = str(t.get("symbol", "")).upper()
            if not sym.endswith("USDT"):
                continue
            if "_" in sym:
                continue  # çeyreklik/vadeli kontratları hariç tut, sadece PERPETUAL kalsın
            base = sym[:-4]
            if any(base.endswith(x) for x in exclude):
                continue
            qv = float(t.get("quoteVolume") or t.get("volume") or 0)
            if qv < min_volume:
                continue
            symbols.append({
                "symbol": sym,
                "base": base,
                "volume_24h": qv,
                "price": float(t.get("lastPrice") or 0),
                "change_24h": float(t.get("priceChangePercent") or 0)
            })

        symbols.sort(key=lambda x: x["volume_24h"], reverse=True)
        return symbols
    except Exception as e:
        st.session_state.setdefault("errors", []).append(f"Binance Futures baglanti hatasi: {type(e).__name__}: {e}")
        return []


def get_stock_data(symbol, timeframe="1h"):
    sym = symbol.replace('$', '').strip().upper()
    if not sym.endswith('USDT'):
        sym = f"{sym}USDT"

    tf_config = TIMEFRAME_OPTIONS.get(timeframe, TIMEFRAME_OPTIONS["1h"])
    binance_interval = tf_config["interval"]
    limit = tf_config["limit"]

    try:
        r = BINANCE_SESSION.get(
            f"{BINANCE_BASE}/fapi/v1/klines",
            params={"symbol": sym, "interval": binance_interval, "limit": limit},
            timeout=20, verify=False
        )
        if r.status_code != 200:
            return None

        data = r.json()
        if not isinstance(data, list) or len(data) < 50:
            return None

        rows = []
        for k in data:
            try:
                rows.append({
                    "timestamp": pd.to_datetime(int(k[0]), unit="ms"),
                    "Open": float(k[1]),
                    "High": float(k[2]),
                    "Low": float(k[3]),
                    "Close": float(k[4]),
                    "Volume": float(k[5])
                })
            except Exception:
                continue

        if len(rows) < 50:
            return None

        df = pd.DataFrame(rows)
        df.set_index("timestamp", inplace=True)
        df = df[['Open', 'High', 'Low', 'Close', 'Volume']].dropna()
        return df
    except Exception:
        return None


# ══════════════════════════════════════════════════════════════
# 🎯 SİNYAL TARAMA - PINE SCRIPT İLE BİREBİR UYUMLU (REPAINT YOK!)
# ══════════════════════════════════════════════════════════════
def scan_money_trader(df, settings=None, timeframe="1h"):
    s = settings or DEFAULT_SETTINGS
    if df is None or len(df) < 50:
        return []

    close    = df['Close']
    high     = df['High']
    low      = df['Low']
    volume   = df['Volume']

    # ── Göstergeler ──────────────────────────────────────────
    rsi_val      = calc_rsi(close, s['rsi_period'])
    rsi_ema      = calc_ema(rsi_val, s['ema_period'])
    momentum_val = calc_momentum(close, s['mom_period'])
    vol_ma       = calc_sma(volume, s['vol_ma_period'])
    vol_ratio    = calc_vol_ratio(df, s['vol_ma_period'])
    atr_val      = calc_atr(df, s['atr_period'])

    # RSI Fibonacci 50 seviyesi (Pine Script'teki fib_500_2)
    rsi_top  = rsi_val.rolling(window=s['fib_len']).max()
    rsi_bot  = rsi_val.rolling(window=s['fib_len']).min()
    fib_500  = rsi_top - (rsi_top - rsi_bot) * 0.500

    ha       = calc_heikin_ashi(df)
    ha_close = ha['haClose']
    ha_open  = ha['haOpen']
    ha_high  = ha['haHigh']

    obv_val  = calc_obv(df)
    obv_ema  = calc_ema(obv_val, s['obv_ema_len'])

    pdh = high.shift(1)
    pdl = low.shift(1)

    i      = -1
    i_prev = -2

    # ── Anlık değerler ───────────────────────────────────────
    cur_close    = float(close.iloc[i])
    prev_close   = float(close.iloc[i_prev])
    cur_pdh      = float(pdh.iloc[i])   if not pd.isna(pdh.iloc[i])   else cur_close
    cur_pdl      = float(pdl.iloc[i])   if not pd.isna(pdl.iloc[i])   else cur_close
    cur_vol_r    = float(vol_ratio.iloc[i]) if not pd.isna(vol_ratio.iloc[i]) else 1.0
    cur_vol      = float(volume.iloc[i])
    cur_vol_ma   = float(vol_ma.iloc[i])   if not pd.isna(vol_ma.iloc[i])   else cur_vol
    cur_atr      = float(atr_val.iloc[i])  if not pd.isna(atr_val.iloc[i])  else cur_close * 0.02
    cur_rsi      = float(rsi_val.iloc[i])  if not pd.isna(rsi_val.iloc[i])  else 50.0
    prev_rsi     = float(rsi_val.iloc[i_prev]) if not pd.isna(rsi_val.iloc[i_prev]) else 50.0
    cur_fib500   = float(fib_500.iloc[i])  if not pd.isna(fib_500.iloc[i])  else 50.0
    prev_fib500  = float(fib_500.iloc[i_prev]) if not pd.isna(fib_500.iloc[i_prev]) else 50.0
    cur_mom      = float(momentum_val.iloc[i]) if not pd.isna(momentum_val.iloc[i]) else 0.0
    cur_obv      = float(obv_val.iloc[i])
    prev_obv     = float(obv_val.iloc[i_prev])
    cur_obv_ema  = float(obv_ema.iloc[i])
    prev_obv_ema = float(obv_ema.iloc[i_prev])
    cur_ha_c     = float(ha_close.iloc[i])
    cur_ha_o     = float(ha_open.iloc[i])
    prev_ha_c    = float(ha_close.iloc[i_prev])
    prev_ha_o    = float(ha_open.iloc[i_prev])
    prev_ha_h    = float(ha_high.iloc[i_prev])

    # ══════════════════════════════════════════════════════════
    # 📈 MOMENTUM SİNYALİ (Pine Script: Hacim + Momentum Yükselişi)
    # RSI > 50 crossover + hacim ortalamanın üstünde
    # ══════════════════════════════════════════════════════════
    mom_rsi_cross_up   = (cur_rsi > 50) and (prev_rsi <= 50)    # RSI 50'yi yukarı kesti
    mom_rsi_cross_down = (cur_rsi < 50) and (prev_rsi >= 50)    # RSI 50'yi aşağı kesti
    vol_yukari         = cur_vol > cur_vol_ma * s['tavan_min_vol_ratio']  # hacim artışı
    vol_dusuk          = cur_vol < cur_vol_ma * s['vol_ma_period'] * 0.05  # düşük hacim (sat için)

    momentum_al  = mom_rsi_cross_up  and (cur_vol > cur_vol_ma)
    momentum_sat = mom_rsi_cross_down and (cur_vol > cur_vol_ma)

    # ══════════════════════════════════════════════════════════
    # 📊 FİBO SİNYALİ (Pine Script: AL Fibo / SAT Fibo)
    # RSI Fibonacci 50 seviyesini crossover
    # ══════════════════════════════════════════════════════════
    fibo_al  = (cur_rsi > prev_fib500) and (prev_rsi <= prev_fib500)   # Fib 50'yi yukarı kesti
    fibo_sat = (cur_rsi < prev_fib500) and (prev_rsi >= prev_fib500)   # Fib 50'yi aşağı kesti

    # ══════════════════════════════════════════════════════════
    # 🚀 CONFLUENCE: İKİSİ AYNI MUMDA → EN GÜÇLÜ SİNYAL
    # ══════════════════════════════════════════════════════════
    confluence_al  = momentum_al  and fibo_al
    confluence_sat = momentum_sat and fibo_sat

    # ── Diğer yardımcı sinyaller ─────────────────────────────
    # OBV
    obv_cross_up   = (cur_obv > cur_obv_ema)  and (prev_obv <= prev_obv_ema)
    obv_cross_down = (cur_obv < cur_obv_ema)  and (prev_obv >= prev_obv_ema)

    # Quantum HA
    ha_is_up     = cur_ha_c > cur_ha_o
    ha_was_down  = prev_ha_c <= prev_ha_o
    sert_yukselis = cur_ha_c > prev_ha_h
    govde        = ((cur_ha_c - cur_ha_o) / cur_ha_o * 100) if cur_ha_o > 0 else 0
    mutlak       = abs(govde)
    quantum_al   = ha_is_up and (ha_was_down or sert_yukselis) and mutlak >= s['hassasiyet']

    # Tavan/Destek kırılımı
    auto_high  = high.rolling(window=s['auto_zone_days']).max().iloc[i]
    auto_low   = low.rolling(window=s['auto_zone_days']).min().iloc[i]
    peak_bot   = auto_high * (1 - s['auto_peak_pct'] / 100)
    dip_top    = auto_low  * (1 + s['auto_dip_pct']  / 100)

    tavan_kirilis  = (cur_close > cur_pdh) and (prev_close <= cur_pdh) and \
                     (cur_close > cur_pdh * (1 + s['tavan_breakout_pct']/100)) and \
                     (cur_vol_r >= s['tavan_min_vol_ratio']) and \
                     not (cur_pdh >= peak_bot and cur_pdh <= auto_high)
    destek_kirilis = (cur_close < cur_pdl) and (prev_close >= cur_pdl) and \
                     (cur_close < cur_pdl * (1 - s['sat_breakout_pct']/100)) and \
                     (cur_vol_r >= s['sat_min_vol_ratio']) and \
                     not (cur_pdl >= auto_low and cur_pdl <= dip_top)

    # ══════════════════════════════════════════════════════════
    # SONUÇ: Önce Confluence, sonra tek başına güçlü sinyaller
    # ══════════════════════════════════════════════════════════
    final_signals = []

    # ── AL sinyalleri ─────────────────────────────────────────
    if confluence_al and s['enable_tavan_al']:
        # En güçlü: Momentum + Fibo aynı anda
        body_s   = abs((ha_close - ha_open) / ha_open * 100)
        avg_body = body_s.rolling(10).mean().iloc[i]
        sg       = min(100, (mutlak / (avg_body if avg_body > 0 else 1)) * s['guc_carpan'])
        olas     = min(s['max_olasilik'], s['base_olasilik'] + sg / s['olasilik_artis_bolucu'] + s['ek_olasilik_bonusu'])
        hedef    = cur_close + cur_atr * s['atr_mult']
        final_signals.append({
            "type": "CONFLUENCE_AL",
            "source": "Momentum(RSI50) + AL(Fibo50)",
            "price": cur_close,
            "strength": round(sg, 1),
            "probability": round(olas, 1),
            "hedef_fiyat": round(hedef, 8),
            "beklenti_yuzde": round((hedef - cur_close) / cur_close * 100, 2),
            "atr": round(cur_atr, 8),
            "rsi": round(cur_rsi, 2),
            "fib500": round(cur_fib500, 2),
            "vol_ratio": round(cur_vol_r, 2),
            "direction": "AL",
            "raw_sources": ["MOMENTUM_AL", "FIBO_AL"],
            "timeframe": timeframe
        })

    elif momentum_al and s['enable_tavan_al']:
        # Sadece Momentum AL
        hedef = cur_close + cur_atr * s['atr_mult']
        final_signals.append({
            "type": "MOMENTUM_AL",
            "source": "RSI50 Crossover + Hacim",
            "price": cur_close,
            "strength": 70.0,
            "probability": round(s['base_olasilik'], 1),
            "hedef_fiyat": round(hedef, 8),
            "beklenti_yuzde": round((hedef - cur_close) / cur_close * 100, 2),
            "atr": round(cur_atr, 8),
            "rsi": round(cur_rsi, 2),
            "fib500": round(cur_fib500, 2),
            "vol_ratio": round(cur_vol_r, 2),
            "direction": "AL",
            "raw_sources": ["MOMENTUM_AL"],
            "timeframe": timeframe
        })

    elif fibo_al and s['enable_tavan_al']:
        # Sadece Fibo AL
        hedef = cur_close + cur_atr * s['atr_mult']
        final_signals.append({
            "type": "FIBO_AL",
            "source": "RSI Fibo50 Crossover",
            "price": cur_close,
            "strength": 70.0,
            "probability": round(s['base_olasilik'], 1),
            "hedef_fiyat": round(hedef, 8),
            "beklenti_yuzde": round((hedef - cur_close) / cur_close * 100, 2),
            "atr": round(cur_atr, 8),
            "rsi": round(cur_rsi, 2),
            "fib500": round(cur_fib500, 2),
            "vol_ratio": round(cur_vol_r, 2),
            "direction": "AL",
            "raw_sources": ["FIBO_AL"],
            "timeframe": timeframe
        })

    # Tavan + OBV + Quantum → ek AL sinyalleri
    for src, label, enabled in [
        (tavan_kirilis, "TAVAN_KIRILIS", s['enable_tavan_al']),
        (obv_cross_up and s['enable_obv_al'], "OBV_AL", s['enable_obv_al']),
        (quantum_al and s['enable_quantum_al'], "QUANTUM_AL", s['enable_quantum_al']),
    ]:
        if src and enabled and not any(sig["direction"] == "AL" and sig["type"] == label for sig in final_signals):
            hedef = cur_close + cur_atr * s['atr_mult']
            final_signals.append({
                "type": label,
                "source": label.replace("_", " "),
                "price": cur_close,
                "strength": 75.0,
                "probability": round(s['base_olasilik'], 1),
                "hedef_fiyat": round(hedef, 8),
                "beklenti_yuzde": round((hedef - cur_close) / cur_close * 100, 2),
                "atr": round(cur_atr, 8),
                "rsi": round(cur_rsi, 2),
                "fib500": round(cur_fib500, 2),
                "vol_ratio": round(cur_vol_r, 2),
                "direction": "AL",
                "raw_sources": [label],
                "timeframe": timeframe
            })

    # ── SAT sinyalleri ────────────────────────────────────────
    if confluence_sat and s['enable_sat']:
        hedef_sat = cur_close - cur_atr * s['atr_mult']
        final_signals.append({
            "type": "CONFLUENCE_SAT",
            "source": "Momentum(RSI50) + SAT(Fibo50)",
            "price": cur_close,
            "strength": 90.0,
            "probability": round(s['max_olasilik'] * 0.9, 1),
            "hedef_fiyat": round(hedef_sat, 8),
            "beklenti_yuzde": round((cur_close - hedef_sat) / cur_close * 100, 2),
            "atr": round(cur_atr, 8),
            "rsi": round(cur_rsi, 2),
            "fib500": round(cur_fib500, 2),
            "vol_ratio": round(cur_vol_r, 2),
            "direction": "SAT",
            "raw_sources": ["MOMENTUM_SAT", "FIBO_SAT"],
            "timeframe": timeframe
        })

    elif momentum_sat and s['enable_sat']:
        hedef_sat = cur_close - cur_atr * s['atr_mult']
        final_signals.append({
            "type": "MOMENTUM_SAT",
            "source": "RSI50 Crossunder + Hacim",
            "price": cur_close,
            "strength": 70.0,
            "probability": round(s['base_olasilik'], 1),
            "hedef_fiyat": round(hedef_sat, 8),
            "beklenti_yuzde": round((cur_close - hedef_sat) / cur_close * 100, 2),
            "atr": round(cur_atr, 8),
            "rsi": round(cur_rsi, 2),
            "fib500": round(cur_fib500, 2),
            "vol_ratio": round(cur_vol_r, 2),
            "direction": "SAT",
            "raw_sources": ["MOMENTUM_SAT"],
            "timeframe": timeframe
        })

    elif fibo_sat and s['enable_sat']:
        hedef_sat = cur_close - cur_atr * s['atr_mult']
        final_signals.append({
            "type": "FIBO_SAT",
            "source": "RSI Fibo50 Crossunder",
            "price": cur_close,
            "strength": 70.0,
            "probability": round(s['base_olasilik'], 1),
            "hedef_fiyat": round(hedef_sat, 8),
            "beklenti_yuzde": round((cur_close - hedef_sat) / cur_close * 100, 2),
            "atr": round(cur_atr, 8),
            "rsi": round(cur_rsi, 2),
            "fib500": round(cur_fib500, 2),
            "vol_ratio": round(cur_vol_r, 2),
            "direction": "SAT",
            "raw_sources": ["FIBO_SAT"],
            "timeframe": timeframe
        })

    for src, label, enabled in [
        (destek_kirilis, "DESTEK_KIRILIS", s['enable_sat']),
        (obv_cross_down and s['enable_obv_sat'], "OBV_SAT", s['enable_obv_sat']),
    ]:
        if src and enabled and not any(sig["direction"] == "SAT" and sig["type"] == label for sig in final_signals):
            hedef_sat = cur_close - cur_atr * s['atr_mult']
            final_signals.append({
                "type": label,
                "source": label.replace("_", " "),
                "price": cur_close,
                "strength": 75.0,
                "probability": round(s['base_olasilik'], 1),
                "hedef_fiyat": round(hedef_sat, 8),
                "beklenti_yuzde": round((cur_close - hedef_sat) / cur_close * 100, 2),
                "atr": round(cur_atr, 8),
                "rsi": round(cur_rsi, 2),
                "fib500": round(cur_fib500, 2),
                "vol_ratio": round(cur_vol_r, 2),
                "direction": "SAT",
                "raw_sources": [label],
                "timeframe": timeframe
            })

    # Min güç filtresi
    final_signals = [sig for sig in final_signals if sig["strength"] >= s['min_guc_seviyesi']]
    return final_signals


# ══════════════════════════════════════════════════════════════
# 🤖 AI PUANLAMA SİSTEMİ
# ══════════════════════════════════════════════════════════════
def ai_score_signal(signal, df):
    score = signal.get('probability', 70)
    strength = signal.get('strength', 50)
    score = (score + strength) / 2

    vol_ratio = signal.get('vol_ratio', 1.0)
    if vol_ratio > 2.0:
        score += 10
    elif vol_ratio > 1.5:
        score += 5

    rsi = signal.get('rsi', 50)
    direction = signal.get('direction', 'AL')

    if direction == 'AL':
        if 30 < rsi < 70:
            score += 5
        if rsi < 30:
            score += 10
        if rsi > 70:
            score -= 5
    else:
        if 30 < rsi < 70:
            score += 5
        if rsi > 70:
            score += 10
        if rsi < 30:
            score -= 5

    if df is not None and len(df) > 20:
        sma20 = df['Close'].rolling(20).mean().iloc[-1]
        if direction == 'AL' and df['Close'].iloc[-1] > sma20:
            score += 5
        if direction == 'SAT' and df['Close'].iloc[-1] < sma20:
            score += 5

    return min(98, max(0, round(score, 1)))


def get_ai_reason(signal):
    reasons = []
    direction = signal.get('direction', 'AL')
    source = signal.get('source', '')
    raw_sources = signal.get('raw_sources', [])
    timeframe = signal.get('timeframe', '1h')

    reasons.append(f"🔹 {signal['type']} sinyali ({source})")
    reasons.append(f"🔹 Zaman Dilimi: {TIMEFRAME_OPTIONS.get(timeframe, {}).get('label', timeframe)}")

    if raw_sources:
        reasons.append(f"🔹 Tetikleyen sinyaller: {', '.join(raw_sources)}")

    reasons.append("🔹 RSI/Fibo onayı alındı (Pine Script uyumlu - REPAINT YOK)")

    vol_ratio = signal.get('vol_ratio', 1.0)
    if vol_ratio > 1.5:
        reasons.append(f"🔹 Yüksek hacim desteği (x{vol_ratio:.1f})")

    rsi = signal.get('rsi', 50)
    if direction == 'AL' and rsi < 40:
        reasons.append(f"🔹 RSI düşük bölgeden dönüş ({rsi:.1f})")
    elif direction == 'SAT' and rsi > 60:
        reasons.append(f"🔹 RSI yüksek bölgeden dönüş ({rsi:.1f})")

    beklenti = signal.get('beklenti_yuzde', 0)
    if abs(beklenti) > 5:
        reasons.append(f"🔹 Yüksek kâr potansiyeli: %{beklenti:.1f}")

    return reasons


# ══════════════════════════════════════════════════════════════
# 📋 BINANCE FUTURES USDT COİNLERİNİ TARA
# ══════════════════════════════════════════════════════════════
def scan_all_binance(settings, max_coins, min_volume, timeframe, progress_bar=None, status_text=None):
    results = []

    if status_text:
        status_text.text(f"📊 Binance'den coin listesi yükleniyor... (TF: {TIMEFRAME_OPTIONS[timeframe]['label']})")

    symbols = get_binance_usdt_symbols(min_volume=min_volume)

    if not symbols:
        if status_text:
            status_text.text("❌ Binance'den coin listesi alınamadı! (Ağ/API erişimini kontrol edin)")
        return []

    if max_coins is not None:
        symbols = symbols[:max_coins]

    total = len(symbols)

    for idx, coin_data in enumerate(symbols):
        symbol = coin_data["symbol"]
        try:
            if progress_bar:
                progress_bar.progress((idx + 1) / total)
            if status_text:
                status_text.text(f"⏳ [{idx + 1}/{total}] {symbol} taranıyor... (Hacim: ${coin_data['volume_24h']/1e6:.2f}M)")

            df = get_stock_data(symbol, timeframe)
            if df is None or len(df) < 50:
                continue

            signals = scan_money_trader(df, settings, timeframe)

            if signals:
                for sig in signals:
                    ai_score = ai_score_signal(sig, df)
                    reasons = get_ai_reason(sig)

                    results.append({
                        "symbol": symbol,
                        "signal": sig,
                        "ai_score": ai_score,
                        "reasons": reasons,
                        "volume_24h": coin_data["volume_24h"],
                        "timestamp": datetime.now().isoformat()
                    })

            time.sleep(0.15)

        except Exception:
            continue

    if status_text:
        status_text.text(f"✅ TARAMA TAMAMLANDI - {len(results)} GÜÇLÜ SİNYAL bulundu! (TF: {TIMEFRAME_OPTIONS[timeframe]['label']})")

    results.sort(key=lambda x: x['ai_score'], reverse=True)
    return results


# ══════════════════════════════════════════════════════════════
# 📊 AKTİF İŞLEM TAKİP SİSTEMİ
# ══════════════════════════════════════════════════════════════
class ActiveTrades:
    def __init__(self):
        self.trades_file = TRADES_FILE
        self.trades = self.load_trades()

    def load_trades(self):
        try:
            if os.path.exists(self.trades_file):
                with open(self.trades_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
        except Exception:
            pass
        return []

    def save_trades(self):
        try:
            with open(self.trades_file, 'w', encoding='utf-8') as f:
                json.dump(self.trades, f, indent=2, ensure_ascii=False)
        except Exception:
            pass

    def add_trade(self, trade):
        trade['id'] = hashlib.md5(
            f"{trade['symbol']}{trade['entry_price']}{datetime.now().isoformat()}".encode()
        ).hexdigest()[:12]
        trade['entry_time'] = datetime.now().isoformat()
        trade['status'] = 'OPEN'
        trade['current_price'] = trade['entry_price']
        trade['pnl_pct'] = 0
        trade['hedef_tamamlandi'] = False
        trade['stop_tamamlandi'] = False

        if trade.get('direction') == 'AL':
            trade['stop_loss'] = trade['entry_price'] * 0.98
        else:
            trade['stop_loss'] = trade['entry_price'] * 1.02

        self.trades.append(trade)
        self.save_trades()
        return trade

    def update_prices(self):
        for trade in self.trades:
            if trade['status'] != 'OPEN':
                continue
            try:
                df = get_stock_data(trade['symbol'], "1d")
                if df is not None and not df.empty:
                    current_price = float(df['Close'].iloc[-1])
                    trade['current_price'] = current_price

                    if trade.get('direction') == 'AL':
                        trade['pnl_pct'] = ((current_price - trade['entry_price']) / trade['entry_price']) * 100
                    else:
                        trade['pnl_pct'] = ((trade['entry_price'] - current_price) / trade['entry_price']) * 100

                    if trade.get('target_price'):
                        if trade['direction'] == 'AL' and current_price >= trade['target_price']:
                            trade['hedef_tamamlandi'] = True
                        elif trade['direction'] == 'SAT' and current_price <= trade['target_price']:
                            trade['hedef_tamamlandi'] = True

                    if trade.get('stop_loss'):
                        if trade['direction'] == 'AL' and current_price <= trade['stop_loss']:
                            trade['stop_tamamlandi'] = True
                            trade['status'] = 'STOPPED'
                        elif trade['direction'] == 'SAT' and current_price >= trade['stop_loss']:
                            trade['stop_tamamlandi'] = True
                            trade['status'] = 'STOPPED'
            except Exception:
                pass

        self.save_trades()

    def close_trade(self, trade_id):
        for trade in self.trades:
            if trade['id'] == trade_id:
                trade['status'] = 'CLOSED'
                trade['close_time'] = datetime.now().isoformat()
                self.save_trades()
                return True
        return False

    def get_open_trades(self):
        return [t for t in self.trades if t['status'] == 'OPEN']

    def get_summary(self):
        open_trades = self.get_open_trades()
        closed_trades = [t for t in self.trades if t['status'] in ['CLOSED', 'STOPPED']]

        total_pnl = sum(t['pnl_pct'] for t in self.trades if t['status'] != 'OPEN')
        winning = sum(1 for t in closed_trades if t.get('hedef_tamamlandi'))
        losing = sum(1 for t in closed_trades if t.get('stop_tamamlandi'))

        return {
            "open_count": len(open_trades),
            "total_count": len(self.trades),
            "closed_count": len(closed_trades),
            "winning": winning,
            "losing": losing,
            "win_rate": (winning / (winning + losing) * 100) if (winning + losing) > 0 else 0,
            "total_pnl": round(total_pnl, 2)
        }


# ══════════════════════════════════════════════════════════════
# 🖥️ STREAMLIT ARAYÜZÜ
# ══════════════════════════════════════════════════════════════
st.set_page_config(
    page_title="MONEY TRADER - Binance Futures Tarama Paneli",
    page_icon="💰",
    layout="wide"
)

# --- Session state başlatma ---
if "settings" not in st.session_state:
    st.session_state.settings = DEFAULT_SETTINGS.copy()
if "scan_results" not in st.session_state:
    st.session_state.scan_results = []
if "trades_manager" not in st.session_state:
    st.session_state.trades_manager = ActiveTrades()
if "last_scan_tf" not in st.session_state:
    st.session_state.last_scan_tf = DEFAULT_SETTINGS["default_timeframe"]

trades_mgr = st.session_state.trades_manager

st.markdown("""
<style>
    .stApp { background-color: #ffffff; }
    div[data-testid="stMetricValue"] { color: #ffd700; }
</style>
""", unsafe_allow_html=True)

st.title("💰 MONEY TRADER — Binance Futures Sinyal Paneli")
st.caption("🎯 Sadece GÜÇLÜ AL/SAT sinyalleri • ✅ Pine Script ile birebir uyumlu RSI/Fibo onayı • ⚠️ REPAINT YOK — sadece son mumda gelen sinyaller • 📡 Veri Kaynağı: Binance Futures (USDT-M Perpetual)")

if "futures_conn_notice_shown" not in st.session_state:
    st.session_state.futures_conn_notice_shown = True
    st.info(
        "ℹ️ Bu panel Binance Futures API'sini kullanır. Eğer tarama sırasında sürekli "
        "'coin listesi alınamadı' hatası görürseniz, barındırma sunucusunun bulunduğu "
        "bölgeden kaynaklanan bir erişim kısıtlaması olabilir — bu durumda bana bildirin."
    )

# ══════════════════════════════════════════════════════════════
# SIDEBAR — Ayarlar
# ══════════════════════════════════════════════════════════════
with st.sidebar:
    st.header("⚙️ Tarama Ayarları")

    timeframe = st.selectbox(
        "Zaman Dilimi",
        options=list(TIMEFRAME_OPTIONS.keys()),
        format_func=lambda x: TIMEFRAME_OPTIONS[x]["label"],
        index=list(TIMEFRAME_OPTIONS.keys()).index(st.session_state.settings.get("default_timeframe", "1h"))
    )

    max_coins = st.number_input(
        "Maksimum Coin Sayısı (tarama süresini etkiler)",
        min_value=10, max_value=3000, value=150, step=10,
        help="Yüksek değerler daha kapsamlı ama daha yavaş tarama demektir. Online barındırmada zaman aşımını önlemek için makul bir değer seçin."
    )

    min_volume = st.number_input(
        "Min 24 Saatlik Hacim (USDT)",
        min_value=0, value=500_000, step=100_000
    )

    st.divider()
    st.markdown("### 🎯 Sinyal Filtresi")
    sinyal_filtresi = st.radio(
        "Hangi sinyalleri göster?",
        options=[
            "🚀 Sadece CONFLUENCE (Momentum + Fibo aynı anda)",
            "📈 Momentum + Fibo + Confluence (hepsi)",
            "🔧 Tüm Sinyaller",
        ],
        index=0,
        help="Confluence = RSI 50 crossover (Momentum) VE RSI Fibo 50 crossover aynı mumda tetiklenir. En güçlü sinyal budur."
    )
    if "sinyal_filtresi" not in st.session_state:
        st.session_state.sinyal_filtresi = sinyal_filtresi
    st.session_state.sinyal_filtresi = sinyal_filtresi

    st.divider()
    with st.expander("🎚️ Gelişmiş Sinyal Ayarları"):
        s = st.session_state.settings
        s['enable_tavan_al'] = st.checkbox("Tavan Adayı AL Sinyali", value=s['enable_tavan_al'])
        s['enable_sat'] = st.checkbox("Sat Sinyali", value=s['enable_sat'])
        s['enable_quantum_al'] = st.checkbox("Quantum AL Sinyali", value=s['enable_quantum_al'])
        s['enable_obv_al'] = st.checkbox("OBV AL Sinyali", value=s['enable_obv_al'])
        s['enable_obv_sat'] = st.checkbox("OBV SAT Sinyali", value=s['enable_obv_sat'])

        st.markdown("**RSI & Fibo (Pine Uyumlu)**")
        s['rsi_period'] = st.number_input("RSI Periyodu", value=s['rsi_period'], min_value=2, max_value=100)
        s['ema_period'] = st.number_input("EMA Periyodu", value=s['ema_period'], min_value=2, max_value=100)
        s['mom_period'] = st.number_input("Momentum Periyodu", value=s['mom_period'], min_value=1, max_value=100)
        s['vol_ma_period'] = st.number_input("Hacim MA Periyodu", value=s['vol_ma_period'], min_value=2, max_value=200)
        s['fib_len'] = st.number_input("RSI Fibonacci Lookback", value=s['fib_len'], min_value=2, max_value=200)

        st.markdown("**Tavan / Sat**")
        s['tavan_breakout_pct'] = st.number_input("Tavan Min Kırılım %", value=float(s['tavan_breakout_pct']), step=0.1, format="%.1f")
        s['tavan_min_vol_ratio'] = st.number_input("Tavan Min Hacim Çarpanı", value=float(s['tavan_min_vol_ratio']), step=0.1, format="%.1f")
        s['sat_breakout_pct'] = st.number_input("Sat Min Kırılım %", value=float(s['sat_breakout_pct']), step=0.1, format="%.1f")
        s['sat_min_vol_ratio'] = st.number_input("Sat Min Hacim Çarpanı", value=float(s['sat_min_vol_ratio']), step=0.1, format="%.1f")

        st.markdown("**Güç ve Olasılık**")
        s['hassasiyet'] = st.number_input("Min Momentum Eşiği (%)", value=float(s['hassasiyet']), step=0.05, format="%.2f")
        s['atr_period'] = st.number_input("Hedef ATR Periyodu", value=s['atr_period'], min_value=2, max_value=100)
        s['atr_mult'] = st.number_input("Hedef ATR Çarpanı", value=float(s['atr_mult']), step=0.1, format="%.1f")
        s['min_guc_seviyesi'] = st.number_input("Min Güç Seviyesi (%)", value=float(s['min_guc_seviyesi']), step=1.0, format="%.0f")

    st.divider()
    scan_clicked = st.button("🔍 TÜMÜNÜ TARA", type="primary", use_container_width=True)

# ══════════════════════════════════════════════════════════════
# TARAMA ÇALIŞTIRMA
# ══════════════════════════════════════════════════════════════
if scan_clicked:
    st.session_state.errors = []
    st.session_state.last_scan_tf = timeframe
    progress_bar = st.progress(0)
    status_text = st.empty()
    with st.spinner("Taranıyor..."):
        results = scan_all_binance(
            st.session_state.settings,
            max_coins=max_coins,
            min_volume=min_volume,
            timeframe=timeframe,
            progress_bar=progress_bar,
            status_text=status_text
        )
    st.session_state.scan_results = results
    progress_bar.empty()

if st.session_state.get("errors"):
    with st.expander("🔧 Hata Detayı (destek için bu kutuyu paylaşın)", expanded=True):
        for err in st.session_state.errors[:5]:
            st.code(err, language=None)
        if "451" in " ".join(st.session_state.errors):
            st.warning(
                "Bu '451' hatası, barındırma sunucusunun bulunduğu bölgeden Binance Futures "
                "API'sine erişimin engellendiği anlamına gelir. Bu durumda farklı bir barındırma "
                "servisine geçmemiz gerekir — bu ekran görüntüsünü Claude'a gösterin."
            )

# ══════════════════════════════════════════════════════════════
# SONUÇLAR
# ══════════════════════════════════════════════════════════════
results = st.session_state.scan_results
tf_label = TIMEFRAME_OPTIONS[st.session_state.last_scan_tf]["label"]

col1, col2, col3, col4 = st.columns(4)
buy_count = sum(1 for r in results if r['signal']['direction'] == 'AL')
sell_count = sum(1 for r in results if r['signal']['direction'] == 'SAT')
avg_score = round(sum(r['ai_score'] for r in results) / len(results), 1) if results else 0

col1.metric("Toplam Güçlü Sinyal", len(results))
col2.metric("🟢 AL Sinyalleri", buy_count)
col3.metric("🔴 SAT Sinyalleri", sell_count)
col4.metric("Ortalama AI Puanı", avg_score)

st.subheader(f"📊 Güçlü Sinyal Listesi (TF: {tf_label})")

if not results:
    st.info("⚠️ Henüz sinyal yok. Soldaki '🔍 TÜMÜNÜ TARA' butonuna basarak taramayı başlatın.")
else:
    # Sidebar'daki sinyal filtresi uygula
    sf = st.session_state.get("sinyal_filtresi", "🔧 Tüm Sinyaller")
    confluence_types = {"CONFLUENCE_AL", "CONFLUENCE_SAT"}
    momentum_fibo_types = {"CONFLUENCE_AL", "CONFLUENCE_SAT", "MOMENTUM_AL", "MOMENTUM_SAT", "FIBO_AL", "FIBO_SAT"}

    if "Sadece CONFLUENCE" in sf:
        filtered_by_type = [r for r in results if r['signal']['type'] in confluence_types]
    elif "Momentum + Fibo" in sf:
        filtered_by_type = [r for r in results if r['signal']['type'] in momentum_fibo_types]
    else:
        filtered_by_type = results

    confluence_count = sum(1 for r in filtered_by_type if r['signal']['type'] in confluence_types)
    if confluence_count > 0:
        st.success(f"🚀 {confluence_count} adet CONFLUENCE sinyali bulundu! (Momentum + Fibo aynı anda)")

    filter_choice = st.radio("Yön Filtresi:", ["Tümü", "Sadece AL 🟢", "Sadece SAT 🔴"], horizontal=True)

    filtered = filtered_by_type
    if filter_choice == "Sadece AL 🟢":
        filtered = [r for r in filtered_by_type if r['signal']['direction'] == 'AL']
    elif filter_choice == "Sadece SAT 🔴":
        filtered = [r for r in filtered_by_type if r['signal']['direction'] == 'SAT']

    table_rows = []
    for r in filtered:
        sig = r['signal']
        table_rows.append({
            "Sembol": r['symbol'],
            "Sinyal": "🚀 CONFLUENCE" if "CONFLUENCE" in sig['type'] else ("📈 MOMENTUM" if "MOMENTUM" in sig['type'] else ("📊 FIBO" if "FIBO" in sig['type'] else sig['type'])),
            "Yön": "🟢 AL" if sig['direction'] == 'AL' else "🔴 SAT",
            "Fiyat": sig['price'],
            "Hedef": sig['hedef_fiyat'],
            "Beklenti %": sig['beklenti_yuzde'],
            "Olasılık %": round(sig['probability'], 1),
            "AI Puan": r['ai_score'],
            "RSI": sig['rsi'],
            "Hacim Oranı": sig['vol_ratio'],
            "Kaynak": sig['source'],
        })

    df_display = pd.DataFrame(table_rows)
    st.dataframe(df_display, use_container_width=True, hide_index=True)

    csv = df_display.to_csv(index=False).encode('utf-8-sig')
    st.download_button(
        "💾 CSV Olarak İndir",
        data=csv,
        file_name=f"money_trader_binance_rapor_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv",
        mime="text/csv"
    )

    st.markdown("#### 🚀 En Güçlü Adaylar — Detay ve İşleme Ekleme")
    for idx, r in enumerate(filtered[:15]):
        sig = r['signal']
        emoji = "🟢" if sig['direction'] == 'AL' else "🔴"
        with st.expander(f"{emoji} {r['symbol']} — {sig['type']} — AI Puan: {r['ai_score']}"):
            c1, c2 = st.columns([2, 1])
            with c1:
                for reason in r['reasons']:
                    st.write(reason)
            with c2:
                st.write(f"**Fiyat:** {sig['price']}")
                st.write(f"**Hedef:** {sig['hedef_fiyat']} (%{sig['beklenti_yuzde']})")
                st.write(f"**Olasılık:** %{sig['probability']:.1f}")
                if st.button("➕ İşleme Ekle", key=f"add_trade_{r['symbol']}_{idx}"):
                    trades_mgr.add_trade({
                        "symbol": r['symbol'],
                        "entry_price": sig['price'],
                        "target_price": sig['hedef_fiyat'],
                        "direction": sig['direction'],
                        "source": sig['source'],
                        "timeframe": sig['timeframe'],
                    })
                    st.success(f"{r['symbol']} aktif işlemlere eklendi!")
                    st.rerun()

# ══════════════════════════════════════════════════════════════
# AKTİF İŞLEMLER
# ══════════════════════════════════════════════════════════════
st.divider()
st.subheader("📈 Aktif İşlemler")

summary = trades_mgr.get_summary()
sc1, sc2, sc3, sc4 = st.columns(4)
sc1.metric("Açık İşlem", summary['open_count'])
sc2.metric("Toplam İşlem", summary['total_count'])
sc3.metric("Kazanma Oranı", f"%{summary['win_rate']:.1f}")
sc4.metric("Toplam PnL", f"%{summary['total_pnl']:.2f}")

if st.button("🔄 Fiyatları Güncelle"):
    with st.spinner("Fiyatlar güncelleniyor..."):
        trades_mgr.update_prices()
    st.rerun()

open_trades = trades_mgr.get_open_trades()
if not open_trades:
    st.caption("Açık işlem yok.")
else:
    for t in open_trades:
        cols = st.columns([2, 1, 1, 1, 1, 1, 1])
        cols[0].write(f"**{t['symbol']}** ({t['direction']})")
        cols[1].write(f"Giriş: {t['entry_price']:.6f}")
        cols[2].write(f"Şu an: {t.get('current_price', t['entry_price']):.6f}")
        pnl = t.get('pnl_pct', 0)
        pnl_color = "🟢" if pnl >= 0 else "🔴"
        cols[3].write(f"{pnl_color} %{pnl:.2f}")
        cols[4].write(f"Hedef: {t.get('target_price', 0):.6f}")
        cols[5].write(f"Stop: {t.get('stop_loss', 0):.6f}")
        if cols[6].button("❌ Kapat", key=f"close_{t['id']}"):
            trades_mgr.close_trade(t['id'])
            st.rerun()

st.divider()
st.caption("MONEY TRADER Web Paneli — orijinal tkinter masaüstü uygulamasının Streamlit sürümüdür. Yatırım tavsiyesi değildir.")
