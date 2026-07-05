# -*- coding: utf-8 -*-
"""
MONEY TRADER - ANI HACİM ARTIŞI TARAMASI (BINANCE FUTURES)
Kapanmamış mumda, o ana kadar gelen hacmi mumun geçen süresine göre normalize ederek
"bu mumda normaldekinin kaç katı hacim geliyor?" sorusunu yanıtlar.
Fiyat yönünden bağımsız — sadece hacim anormalliği arar.
"""
import time
from datetime import datetime

import pandas as pd
import requests
import streamlit as st
import urllib3
import warnings

urllib3.disable_warnings()
warnings.filterwarnings('ignore')

st.set_page_config(
    page_title="Ani Hacim Artışı (Futures) - MONEY TRADER",
    page_icon="📊",
    layout="wide"
)

BINANCE_BASE = "https://fapi.binance.com"
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "MoneyTrader-VolumeSpike-Futures/1.0"})

TIMEFRAME_OPTIONS = {
    "1m":  {"label": "1 Dakika",  "interval": "1m"},
    "3m":  {"label": "3 Dakika",  "interval": "3m"},
    "5m":  {"label": "5 Dakika",  "interval": "5m"},
    "15m": {"label": "15 Dakika", "interval": "15m"},
    "30m": {"label": "30 Dakika", "interval": "30m"},
    "1h":  {"label": "1 Saat",    "interval": "1h"},
    "4h":  {"label": "4 Saat",    "interval": "4h"},
    "1d":  {"label": "Günlük",    "interval": "1d"},
}


def get_server_time_ms():
    try:
        r = SESSION.get(f"{BINANCE_BASE}/fapi/v1/time", timeout=10, verify=False)
        r.raise_for_status()
        return int(r.json()["serverTime"])
    except Exception:
        return int(time.time() * 1000)


def get_symbols(min_volume=0):
    try:
        r = SESSION.get(f"{BINANCE_BASE}/fapi/v1/ticker/24hr", timeout=30, verify=False)
        if r.status_code != 200:
            return [], f"HTTP {r.status_code}: {r.text[:300]}"
        data = r.json()
        if not isinstance(data, list):
            return [], f"Beklenmeyen yanit: {str(data)[:200]}"

        exclude = ("3L", "3S", "5L", "5S", "UP", "DOWN", "BULL", "BEAR")
        symbols = []
        for t in data:
            sym = str(t.get("symbol", "")).upper()
            if not sym.endswith("USDT") or "_" in sym:
                continue
            base = sym[:-4]
            if any(base.endswith(x) for x in exclude):
                continue
            qv = float(t.get("quoteVolume") or 0)
            if qv < min_volume:
                continue
            symbols.append({
                "symbol": sym,
                "volume_24h": qv,
                "price": float(t.get("lastPrice") or 0),
                "change_24h": float(t.get("priceChangePercent") or 0),
            })

        symbols.sort(key=lambda x: x["volume_24h"], reverse=True)
        return symbols, None
    except Exception as e:
        return [], f"{type(e).__name__}: {e}"


def get_volume_spike(symbol, interval, server_time_ms, lookback=20, min_elapsed_pct=5):
    try:
        limit = lookback + 5  # son kapanmamış mum + lookback kadar kapanmış + güvenlik payı
        r = SESSION.get(
            f"{BINANCE_BASE}/fapi/v1/klines",
            params={"symbol": symbol, "interval": interval, "limit": limit},
            timeout=15, verify=False
        )
        if r.status_code != 200:
            return None
        data = r.json()
        if not isinstance(data, list) or len(data) < 5:
            return None

        last       = data[-1]
        open_time  = int(last[0])
        open_p     = float(last[1])
        high_p     = float(last[2])
        low_p      = float(last[3])
        close_p    = float(last[4])
        cur_vol    = float(last[5])
        close_time = int(last[6])

        # Mum kapanmış mı?
        if server_time_ms >= close_time:
            return None

        duration_ms  = close_time - open_time
        elapsed_ms   = server_time_ms - open_time
        elapsed_frac = max(0.0001, min(1.0, elapsed_ms / duration_ms))
        elapsed_pct  = elapsed_frac * 100

        if elapsed_pct < min_elapsed_pct:
            return None

        # Önceki KAPANMIŞ mumların ortalama hacmi
        closed_vols = [float(k[5]) for k in data[:-1][-lookback:]]
        if len(closed_vols) < 3:
            return None
        avg_vol = sum(closed_vols) / len(closed_vols)
        if avg_vol <= 0:
            return None

        # Projeksiyon: bu hızla giderse tam mum kaç birim hacim üretir?
        projected_vol = cur_vol / elapsed_frac
        norm_ratio    = round(projected_vol / avg_vol, 2)

        # Anlık oran: o ana kadar gelen gerçek hacim / ortalama
        raw_ratio = round(cur_vol / avg_vol, 2)

        pct_change    = round(((close_p - open_p) / open_p * 100) if open_p > 0 else 0, 2)
        direction     = "🟢 AL" if pct_change >= 0 else "🔴 SAT"
        remaining_min = max(0, round((close_time - server_time_ms) / 60000))

        return {
            "symbol":        symbol,
            "direction":     direction,
            "pct_change":    pct_change,
            "current_price": close_p,
            "open_price":    open_p,
            "high":          high_p,
            "low":           low_p,
            "cur_vol":       round(cur_vol, 2),
            "avg_vol":       round(avg_vol, 2),
            "norm_ratio":    norm_ratio,
            "raw_ratio":     raw_ratio,
            "elapsed_pct":   round(elapsed_pct, 1),
            "remaining_min": remaining_min,
        }

    except Exception:
        return None


def scan(timeframe, min_norm_ratio, min_elapsed_pct, lookback,
         max_coins, min_volume_24h, progress_bar=None, status_text=None):

    server_time_ms = get_server_time_ms()
    interval       = TIMEFRAME_OPTIONS[timeframe]["interval"]

    if status_text:
        status_text.text("📊 Binance Futures coin listesi yükleniyor...")

    symbols, err = get_symbols(min_volume=min_volume_24h)

    if err:
        st.error(f"❌ API Hatası: {err}")
        if "451" in str(err):
            st.warning("451 hatası: Frankfurt (Render) sunucusundan çalıştırmanız gerekiyor.")
        return []

    if not symbols:
        st.warning("Kriterlere uyan coin bulunamadı.")
        return []

    if max_coins:
        symbols = symbols[:max_coins]

    total   = len(symbols)
    results = []

    for idx, coin in enumerate(symbols):
        symbol = coin["symbol"]
        if progress_bar:
            progress_bar.progress((idx + 1) / total)
        if status_text:
            status_text.text(f"⏳ [{idx+1}/{total}] {symbol} — bulundu: {len(results)}")

        try:
            res = get_volume_spike(symbol, interval, server_time_ms, lookback, min_elapsed_pct)
            if res and res["norm_ratio"] >= min_norm_ratio:
                res["volume_24h"] = coin["volume_24h"]
                results.append(res)
            time.sleep(0.08)
        except Exception:
            continue

    if status_text:
        status_text.text(f"✅ Tamamlandı — {len(results)} coinde ani hacim artışı!")

    results.sort(key=lambda x: x["norm_ratio"], reverse=True)
    return results


# ══════════════════════════════════════════════════════════════
# ARAYÜZ
# ══════════════════════════════════════════════════════════════
st.title("📊 Ani Hacim Artışı Taraması (Binance Futures)")
st.caption("Kapanmamış mumda gelen hacmi süreye göre normalize eder — 'bu mumda normaldekinin kaç katı hacim var?' sorusunu yanıtlar.")

st.info(
    "**Normalize Hacim Oranı nasıl hesaplanır?**  \n"
    "4 saatlik mumun %25'i geçmişken 100 birim hacim geldiyse → tahmini tam mum: 400 birim.  \n"
    "Son 20 mumun ortalaması 80 birim ise → **Normalize Oran = 400/80 = 5.0x**  \n"
    "Mum kapanmadan normalin 5 katı hacim patlaması var demektir."
)

with st.sidebar:
    st.header("⚙️ Hacim Tarama Ayarları")

    timeframe = st.selectbox(
        "Zaman Dilimi",
        options=list(TIMEFRAME_OPTIONS.keys()),
        format_func=lambda x: TIMEFRAME_OPTIONS[x]["label"],
        index=5
    )

    min_norm_ratio = st.number_input(
        "Min Normalize Hacim Oranı (x)",
        min_value=1.0, max_value=50.0, value=1.5, step=0.5,
        help="1.5x ile başlayın, sonuç yoksa 1.2'ye düşürün."
    )

    min_elapsed_pct = st.number_input(
        "Min Mum İlerleme %",
        min_value=0, max_value=90, value=5, step=5,
        help="0 yaparsanız hiç filtre uygulanmaz, tüm açık mumlar değerlendirilir."
    )

    lookback = st.number_input(
        "Ortalama İçin Önceki Mum Sayısı",
        min_value=3, max_value=50, value=20, step=5
    )

    max_coins = st.number_input(
        "Maksimum Coin Sayısı",
        min_value=10, max_value=2000, value=200, step=10
    )

    min_volume_24h = st.number_input(
        "Min 24 Saatlik Hacim (USDT)",
        min_value=0, value=500_000, step=500_000
    )

    st.divider()
    scan_clicked = st.button("📊 HACİM TARAMASI BAŞLAT", type="primary", use_container_width=True)
    auto_refresh = st.checkbox("60 saniyede bir otomatik yenile", value=False)

if "vol_results" not in st.session_state:
    st.session_state.vol_results = []

if scan_clicked:
    progress_bar = st.progress(0)
    status_text  = st.empty()
    with st.spinner("Taranıyor..."):
        st.session_state.vol_results = scan(
            timeframe, min_norm_ratio, min_elapsed_pct, lookback,
            max_coins, min_volume_24h, progress_bar, status_text
        )
    progress_bar.empty()

results   = st.session_state.vol_results
tf_label  = TIMEFRAME_OPTIONS[timeframe]["label"]

c1, c2, c3, c4 = st.columns(4)
buy_c     = sum(1 for r in results if "AL"  in r["direction"])
sell_c    = sum(1 for r in results if "SAT" in r["direction"])
top_ratio = max((r["norm_ratio"] for r in results), default=0)

c1.metric("Toplam Tespit", len(results))
c2.metric("🟢 Yükselen",  buy_c)
c3.metric("🔴 Düşen",     sell_c)
c4.metric("En Yüksek Oran", f"{top_ratio}x")

st.subheader(f"📋 Ani Hacim Listesi  (TF: {tf_label} | Min: {min_norm_ratio}x)")

if not results:
    st.info("Henüz tarama yapılmadı veya kriter sağlayan coin bulunamadı. "
            "Sonuç yoksa 'Min Normalize Hacim Oranı'nı düşürün (1.2x ile deneyin) "
            "ya da 'Min Mum İlerleme %'ni 0'a çekin.")
else:
    filtre = st.radio("Filtrele:", ["Tümü", "Sadece Yükselenler 🟢", "Sadece Düşenler 🔴"], horizontal=True)

    filtered = results
    if filtre == "Sadece Yükselenler 🟢":
        filtered = [r for r in results if "AL"  in r["direction"]]
    elif filtre == "Sadece Düşenler 🔴":
        filtered = [r for r in results if "SAT" in r["direction"]]

    rows = []
    for r in filtered:
        rows.append({
            "Sembol":            r["symbol"],
            "Yön":               r["direction"],
            "Fiyat Değişim %":   r["pct_change"],
            "Norm. Oran (proj)": f"{r['norm_ratio']}x",
            "Anlık Oran":        f"{r['raw_ratio']}x",
            "Şu An Fiyat":       r["current_price"],
            "Mum İlerleme %":    r["elapsed_pct"],
            "Kalan (dk)":        r["remaining_min"],
            "24s Hacim":         f"${r['volume_24h']/1e6:.1f}M",
        })

    df_out = pd.DataFrame(rows)
    st.dataframe(df_out, use_container_width=True, hide_index=True)

    csv = df_out.to_csv(index=False).encode("utf-8-sig")
    st.download_button(
        "💾 CSV İndir", data=csv,
        file_name=f"ani_hacim_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv",
        mime="text/csv"
    )

    st.caption(
        "**Norm. Oran (proj):** mevcut hızla giderse tahmini tam mum hacmi / önceki N mum ortalaması.  "
        "**Anlık Oran:** o ana kadar gelen gerçek hacim / ortalama. "
        "Mum kapanmadığı için her iki değer de değişmeye devam eder."
    )

if auto_refresh:
    time.sleep(60)
    st.rerun()
