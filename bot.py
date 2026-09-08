"""
Hacim + 4H Tepe Kırılım Tarama Botu
------------------------------------
Mantık:
1) Binance Futures'ta her USDT perpetual coin için 15 dakikalık mumlarda
   hacim, son 20 periyodun ortalama hacminin en az %5 üzerine çıkmış mı bak.
2) Eğer hacim şartı sağlanıyorsa, aynı coin için 4 saatlik grafikte son 20
   barın en yüksek noktasını (swing high / "tepe") bul.
3) Güncel fiyat bu tepeyi geçmişse (kırmışsa), Telegram'a sinyal gönder.

Ayarlar en üstteki CONFIG bölümünden değiştirilebilir.
"""

import time
import logging
import requests
from datetime import datetime, timezone

# ============================== CONFIG ==============================
BINANCE_FAPI = "https://fapi.binance.com"

VOLUME_LOOKBACK = 20          # 15dk hacim ortalaması için bar sayısı
VOLUME_THRESHOLD_PCT = 5.0    # ortalamanın üzerine gereken minimum yüzde (%5)

PIVOT_LOOKBACK = 20           # 4h tepe için bar sayısı
MIN_24H_USDT_VOLUME = 3_000_000  # çok düşük hacimli/illiquid coinleri ele

CHECK_INTERVAL_SECONDS = 15 * 60   # 15 dakikada bir tüm listeyi tara
REQUEST_SLEEP = 0.05                # rate-limit için istekler arası bekleme

TELEGRAM_BOT_TOKEN = "BURAYA_TELEGRAM_BOT_TOKEN"
TELEGRAM_CHAT_ID = "BURAYA_CHAT_ID"

# Sinyal aynı coin için tekrar tekrar gelmesin diye kısa süreli hafıza
ALERTED_COOLDOWN_SECONDS = 4 * 60 * 60  # aynı coin için 4 saat tekrar gönderme
_last_alert_time = {}

# ============================== LOGGING ==============================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("hacim_tepe_bot")

# ============================ TELEGRAM ============================
def send_telegram(message: str):
    if not TELEGRAM_BOT_TOKEN or "BURAYA" in TELEGRAM_BOT_TOKEN:
        log.warning("Telegram token ayarlanmamış, sadece log'a yazılıyor.")
        log.info(message)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        resp = requests.post(url, data={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "HTML"
        }, timeout=10)
        if resp.status_code != 200:
            log.error(f"Telegram gönderilemedi. HTTP {resp.status_code}: {resp.text}")
    except Exception as e:
        log.error(f"Telegram gönderilemedi (exception): {e}")


# ============================ BINANCE API ============================
def get_usdt_perpetual_symbols():
    """Tüm USDT perpetual futures sembollerini çeker."""
    url = f"{BINANCE_FAPI}/fapi/v1/exchangeInfo"
    resp = requests.get(url, timeout=15)
    resp.raise_for_status()
    data = resp.json()

    symbols = []
    for s in data["symbols"]:
        if (s["contractType"] == "PERPETUAL"
                and s["quoteAsset"] == "USDT"
                and s["status"] == "TRADING"):
            symbols.append(s["symbol"])
    return symbols


def get_24h_quote_volume(symbol: str):
    url = f"{BINANCE_FAPI}/fapi/v1/ticker/24hr"
    resp = requests.get(url, params={"symbol": symbol}, timeout=10)
    resp.raise_for_status()
    data = resp.json()
    return float(data["quoteVolume"])


def get_klines(symbol: str, interval: str, limit: int):
    """
    Binance kline formatı:
    [ openTime, open, high, low, close, volume, closeTime, ... ]
    """
    url = f"{BINANCE_FAPI}/fapi/v1/klines"
    resp = requests.get(url, params={
        "symbol": symbol,
        "interval": interval,
        "limit": limit
    }, timeout=10)
    resp.raise_for_status()
    return resp.json()


# ============================ SİNYAL MANTIĞI ============================
def check_volume_spike(symbol: str):
    """
    15dk mumlarında: son KAPANMIŞ mumun hacmi, önceki 20 mumun
    ortalamasının en az %VOLUME_THRESHOLD_PCT üzerinde mi?
    Dönen: (sinyal_var_mi, guncel_hacim, ortalama_hacim)
    """
    klines = get_klines(symbol, "15m", VOLUME_LOOKBACK + 2)
    if len(klines) < VOLUME_LOOKBACK + 2:
        return False, None, None

    closed_klines = klines[:-1]  # son mum muhtemelen henüz kapanmadı, çıkar
    trigger_candle = closed_klines[-1]
    avg_window = closed_klines[-(VOLUME_LOOKBACK + 1):-1]

    trigger_volume = float(trigger_candle[5])
    avg_volume = sum(float(c[5]) for c in avg_window) / len(avg_window)

    if avg_volume == 0:
        return False, trigger_volume, avg_volume

    increase_pct = (trigger_volume - avg_volume) / avg_volume * 100
    return increase_pct >= VOLUME_THRESHOLD_PCT, trigger_volume, avg_volume


def check_4h_breakout(symbol: str):
    """
    4h grafikte son PIVOT_LOOKBACK kapanmış barın en yüksek noktasını (tepe)
    bulur ve güncel fiyatın bu tepeyi geçip geçmediğini kontrol eder.
    Dönen: (kirildi_mi, guncel_fiyat, tepe_seviyesi)
    """
    klines = get_klines(symbol, "4h", PIVOT_LOOKBACK + 2)
    if len(klines) < PIVOT_LOOKBACK + 2:
        return False, None, None

    closed_klines = klines[:-1]
    pivot_window = closed_klines[-PIVOT_LOOKBACK:]
    swing_high = max(float(c[2]) for c in pivot_window)  # high sütunu

    current_price = float(closed_klines[-1][4])  # son kapanmış 4h mumun close'u

    return current_price > swing_high, current_price, swing_high


def scan_symbol(symbol: str, stats: dict):
    try:
        vol_signal, trigger_vol, avg_vol = check_volume_spike(symbol)
        if not vol_signal:
            return

        stats["volume_gecen"] += 1
        log.debug(f"{symbol}: hacim şartı sağlandı, 4h kontrolüne geçiliyor.")

        breakout_signal, price, swing_high = check_4h_breakout(symbol)
        if not breakout_signal:
            return

        stats["kirilim_gecen"] += 1

        # cooldown kontrolü
        now = time.time()
        last = _last_alert_time.get(symbol, 0)
        if now - last < ALERTED_COOLDOWN_SECONDS:
            log.info(f"{symbol}: sinyal şartları sağlandı ama cooldown aktif, atlanıyor.")
            return

        volume_increase_pct = (trigger_vol - avg_vol) / avg_vol * 100
        breakout_pct = (price - swing_high) / swing_high * 100

        message = (
            f"🚀 <b>{symbol}</b> — Hacim + 4H Tepe Kırılımı\n"
            f"15dk Hacim: ortalamanın <b>%{volume_increase_pct:.1f}</b> üzerinde\n"
            f"4H Tepe: {swing_high:.6f}\n"
            f"Güncel Fiyat: {price:.6f} (tepeyi <b>%{breakout_pct:.2f}</b> geçti)\n"
            f"Zaman: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
        )
        send_telegram(message)
        _last_alert_time[symbol] = now
        stats["sinyal"] += 1
        log.info(f"SİNYAL: {symbol} -> hacim +%{volume_increase_pct:.1f}, kırılım +%{breakout_pct:.2f}")

    except Exception as e:
        stats["hata"] += 1
        log.error(f"{symbol} taranırken hata: {type(e).__name__}: {e}")


def run_scan_cycle():
    log.info("Tarama başlıyor...")

    try:
        symbols = get_usdt_perpetual_symbols()
    except Exception as e:
        log.error(f"Sembol listesi çekilemedi: {type(e).__name__}: {e}")
        return

    log.info(f"Toplam {len(symbols)} USDT perpetual sembol bulundu.")

    stats = {"taranan": 0, "hacim_filtresi_gecti": 0, "volume_gecen": 0, "kirilim_gecen": 0, "sinyal": 0, "hata": 0}

    for symbol in symbols:
        try:
            vol24 = get_24h_quote_volume(symbol)
            if vol24 < MIN_24H_USDT_VOLUME:
                continue
        except Exception as e:
            log.error(f"{symbol}: 24h hacim çekilemedi: {type(e).__name__}: {e}")
            continue

        stats["hacim_filtresi_gecti"] += 1
        scan_symbol(symbol, stats)
        stats["taranan"] += 1
        time.sleep(REQUEST_SLEEP)

    log.info(
        f"Tarama bitti: {stats['taranan']} coin tarandı "
        f"(24h hacim filtresini {stats['hacim_filtresi_gecti']} coin geçti), "
        f"15dk hacim şartını {stats['volume_gecen']} coin sağladı, "
        f"4h kırılım şartını {stats['kirilim_gecen']} coin sağladı, "
        f"{stats['sinyal']} sinyal gönderildi, {stats['hata']} hata oluştu."
    )


def main():
    log.info("Hacim + 4H Tepe Kırılım Botu başlatıldı.")
    send_telegram("✅ Hacim + 4H Tepe Kırılım Botu başlatıldı, ilk tarama başlıyor.")
    while True:
        start = time.time()
        run_scan_cycle()
        elapsed = time.time() - start
        sleep_time = max(0, CHECK_INTERVAL_SECONDS - elapsed)
        log.info(f"Sonraki tarama {sleep_time/60:.1f} dakika sonra.")
        time.sleep(sleep_time)


if __name__ == "__main__":
    main()
