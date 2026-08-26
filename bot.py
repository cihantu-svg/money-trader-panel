"""
Basit Tarayıcı Bot - SADECE 3 KRİTER:
1) 15 dakikalık mum
2) GÜNLÜK (24 saatlik) hacim >= 3,000,000 USDT
3) Son 15dk fiyat değişimi >= %7 (mutlak değer, yani hem yükseliş hem düşüş)

Başka HİÇBİR filtre yok (RSI, SMA, trend, breakout vs. YOK).
Binance Futures üzerinde tüm USDT paritelerini tarar.
"""

import time
import requests
from datetime import datetime, timezone

# ============ AYARLAR ============
VOLUME_USDT_MIN = 3_000_000      # minimum GÜNLÜK (24s) hacim ($)
PRICE_CHANGE_MIN = 7.0           # minimum %7 hareket (15dk mumda)
CHECK_INTERVAL_SEC = 60          # kaç saniyede bir tarasın
TELEGRAM_BOT_TOKEN = ""          # kendi token'ını gir
TELEGRAM_CHAT_ID = ""            # kendi chat id'ni gir
# ==================================

BINANCE_FAPI = "https://fapi.binance.com"


def send_telegram(msg: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("[TELEGRAM DEVRE DIŞI]", msg)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": msg}, timeout=10)
    except Exception as e:
        print("Telegram gönderim hatası:", e)


def get_usdt_futures_symbols():
    url = f"{BINANCE_FAPI}/fapi/v1/exchangeInfo"
    data = requests.get(url, timeout=15).json()
    symbols = [
        s["symbol"] for s in data["symbols"]
        if s["symbol"].endswith("USDT") and s.get("contractType") == "PERPETUAL"
        and s.get("status") == "TRADING"
    ]
    return symbols


def get_24h_volumes():
    """Tüm semboller için 24 saatlik quote volume (USDT) sözlüğü döndürür."""
    url = f"{BINANCE_FAPI}/fapi/v1/ticker/24hr"
    data = requests.get(url, timeout=15).json()
    return {item["symbol"]: float(item["quoteVolume"]) for item in data}


def get_last_15m_kline(symbol: str):
    """Kapanmış son 15 dakikalık mumu döndürür."""
    url = f"{BINANCE_FAPI}/fapi/v1/klines"
    params = {"symbol": symbol, "interval": "15m", "limit": 2}
    r = requests.get(url, params=params, timeout=10).json()
    if len(r) < 2:
        return None
    # r[-1] genelde henüz kapanmamış olabilir, kapanmış olan bir öncekini alıyoruz
    kline = r[-2]
    open_price = float(kline[1])
    high_price = float(kline[2])
    low_price = float(kline[3])
    close_price = float(kline[4])
    volume_base = float(kline[5])
    quote_volume = float(kline[7])  # USDT cinsinden hacim
    return {
        "open": open_price,
        "high": high_price,
        "low": low_price,
        "close": close_price,
        "quote_volume": quote_volume,
    }


def scan_once():
    hits = []
    symbols = get_usdt_futures_symbols()
    daily_volumes = get_24h_volumes()  # tek seferde tüm sembollerin günlük hacmi

    for symbol in symbols:
        try:
            daily_vol = daily_volumes.get(symbol, 0.0)
            if daily_vol < VOLUME_USDT_MIN:
                continue  # günlük hacim yetersizse mum verisine bile bakma

            k = get_last_15m_kline(symbol)
            if k is None:
                continue

            change_pct = (k["close"] - k["open"]) / k["open"] * 100

            if abs(change_pct) < PRICE_CHANGE_MIN:
                continue

            hits.append({
                "symbol": symbol,
                "change_pct": change_pct,
                "volume_usdt_24h": daily_vol,
                "close": k["close"],
            })
        except Exception as e:
            print(f"{symbol} hata:", e)
        time.sleep(0.05)  # rate limit için küçük bekleme

    return hits


def format_alert(hit: dict) -> str:
    direction = "🟢 YÜKSELİŞ" if hit["change_pct"] > 0 else "🔴 DÜŞÜŞ"
    return (
        f"{direction} | {hit['symbol']}\n"
        f"15dk Değişim: {hit['change_pct']:.2f}%\n"
        f"Günlük Hacim: ${hit['volume_usdt_24h']:,.0f}\n"
        f"Fiyat: {hit['close']}\n"
        f"Zaman: {datetime.now(timezone.utc).strftime('%H:%M:%S UTC')}"
    )


def main():
    print("Tarayıcı başladı. Kriterler: 15dk mum | Günlük Hacim >= $3M | 15dk Değişim >= %7")
    send_telegram(
        "✅ Tarayıcı başladı.\n"
        f"Kriterler: Günlük Hacim >= ${VOLUME_USDT_MIN:,.0f} | 15dk Değişim >= %{PRICE_CHANGE_MIN}\n"
        "Herhangi bir değişiklik yok, sadece bu 2 kriter aktif."
    )
    already_alerted = set()

    while True:
        try:
            hits = scan_once()
            for hit in hits:
                key = (hit["symbol"], round(hit["change_pct"], 1))
                if key in already_alerted:
                    continue
                already_alerted.add(key)

                msg = format_alert(hit)
                print(msg)
                send_telegram(msg)

            # aynı mumda tekrar alarm atmasın diye set'i belirli aralıkla temizle
            if len(already_alerted) > 500:
                already_alerted.clear()

        except Exception as e:
            print("Tarama döngüsü hatası:", e)

        time.sleep(CHECK_INTERVAL_SEC)


if __name__ == "__main__":
    main()
