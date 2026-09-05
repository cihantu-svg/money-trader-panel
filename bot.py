import sys
import os
import time
import logging
import requests
from datetime import datetime, timezone


# ============================================================
# LOGGING
# ============================================================

_handler = logging.StreamHandler(sys.stdout)
_handler.setFormatter(
    logging.Formatter("%(asctime)s %(levelname)s %(message)s")
)
_handler.flush = sys.stdout.flush

logging.basicConfig(
    level=logging.INFO,
    handlers=[_handler],
    force=True
)

log = logging.getLogger(__name__)


# ============================================================
# SETTINGS / ENV
# ============================================================

BINANCE_FAPI = "https://fapi.binance.com"

# Minimum 24h USDT volume
VOLUME_USDT_MIN = float(
    os.getenv("VOLUME_USDT_MIN", "1000000")
)

# Major levelden minimum uzaklaşma
BREAKOUT_THRESHOLD = float(
    os.getenv("BREAKOUT_THRESHOLD", "5")
)

# Ana tarama sıklığı
SCAN_INTERVAL_SEC = int(
    os.getenv("SCAN_INTERVAL_SEC", "10")
)

# Major seviyeleri kaç saniyede bir yeniden hesaplayalım
LEVEL_REFRESH_SEC = int(
    os.getenv("LEVEL_REFRESH_SEC", "60")
)

# Major level için kaç adet 15m mum
# 288 = 72 saat = 3 gün
MAJOR_LOOKBACK = int(
    os.getenv("MAJOR_LOOKBACK", "288")
)

# Major level için minimum touch
MAJOR_TOUCHES = int(
    os.getenv("MAJOR_TOUCHES", "2")
)

# Aynı seviyenin kabul edileceği tolerans
# 0.005 = %0.5
MAJOR_TOLERANCE = float(
    os.getenv("MAJOR_TOLERANCE", "0.005")
)

# Pivot hassasiyeti
PIVOT_LEFT = int(
    os.getenv("PIVOT_LEFT", "2")
)

PIVOT_RIGHT = int(
    os.getenv("PIVOT_RIGHT", "2")
)

# Kline yenileme
KLINE_REFRESH_SEC = int(
    os.getenv("KLINE_REFRESH_SEC", "60")
)

TELEGRAM_TOKEN = os.getenv(
    "TELEGRAM_TOKEN",
    ""
)

TELEGRAM_CHAT_ID = os.getenv(
    "TELEGRAM_CHAT_ID",
    ""
)

MAX_RETRIES = 3
RETRY_BACKOFF_BASE = 0.5

WINDOW_MS = 15 * 60 * 1000


# ============================================================
# HTTP SESSION
# ============================================================

session = requests.Session()

adapter = requests.adapters.HTTPAdapter(
    pool_connections=20,
    pool_maxsize=20
)

session.mount("https://", adapter)


# ============================================================
# MEMORY
# ============================================================

# symbol -> {
#     support,
#     resistance,
#     updated
# }
major_levels = {}

# symbol -> last price
previous_prices = {}

# symbol -> last kline refresh
last_kline_refresh = {}

# Aynı 15m penceresinde aynı sinyali tekrar gönderme
alerted_support = {}
alerted_resistance = {}


# ============================================================
# HTTP REQUEST
# ============================================================

def _get_with_retry(url, params=None, timeout=15):

    attempt = 0

    while True:

        try:

            r = session.get(
                url,
                params=params,
                timeout=timeout
            )

        except requests.exceptions.RequestException as e:

            attempt += 1

            if attempt > MAX_RETRIES:
                raise

            wait = RETRY_BACKOFF_BASE * (2 ** attempt)

            log.warning(
                f"Network error: {e} | "
                f"{wait:.1f}s sonra tekrar"
            )

            time.sleep(wait)

            continue

        # ----------------------------------------------------
        # SUCCESS
        # ----------------------------------------------------

        if r.status_code == 200:
            return r.json()

        # ----------------------------------------------------
        # 418
        # ----------------------------------------------------

        if r.status_code == 418:

            retry_after = r.headers.get(
                "Retry-After"
            )

            try:
                wait = float(retry_after) if retry_after else 60
            except ValueError:
                wait = 60

            wait += 2

            log.error(
                f"HTTP 418 - Binance IP geçici ban. "
                f"{wait:.0f}s bekleniyor."
            )

            remaining = wait

            while remaining > 0:

                chunk = min(20, remaining)

                time.sleep(chunk)

                remaining -= chunk

                if remaining > 0:

                    log.info(
                        f"[418] kalan ~{remaining:.0f}s"
                    )

            attempt = 0
            continue

        # ----------------------------------------------------
        # 429
        # ----------------------------------------------------

        if r.status_code == 429:

            retry_after = r.headers.get(
                "Retry-After"
            )

            wait = RETRY_BACKOFF_BASE * (
                2 ** attempt
            )

            if retry_after:

                try:
                    wait = max(
                        wait,
                        float(retry_after)
                    )
                except ValueError:
                    pass

            attempt += 1

            log.warning(
                f"HTTP 429 | "
                f"{wait:.1f}s bekle | "
                f"deneme {attempt}"
            )

            time.sleep(wait)

            if attempt > MAX_RETRIES:

                raise Exception(
                    "HTTP 429 - retry limiti"
                )

            continue

        # ----------------------------------------------------
        # OTHER
        # ----------------------------------------------------

        attempt += 1

        wait = RETRY_BACKOFF_BASE * (
            2 ** attempt
        )

        log.warning(
            f"HTTP {r.status_code} | "
            f"{wait:.1f}s sonra tekrar"
        )

        time.sleep(wait)

        if attempt > MAX_RETRIES:

            raise Exception(
                f"HTTP {r.status_code}"
            )


# ============================================================
# TELEGRAM
# ============================================================

def send_telegram(message):

    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:

        log.warning(
            "Telegram ENV eksik."
        )

        return

    url = (
        f"https://api.telegram.org/"
        f"bot{TELEGRAM_TOKEN}/sendMessage"
    )

    try:

        r = session.post(
            url,
            data={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": message
            },
            timeout=10
        )

        if r.status_code != 200:

            log.error(
                f"Telegram HTTP {r.status_code}: "
                f"{r.text[:200]}"
            )

    except Exception as e:

        log.error(
            f"Telegram gönderim hatası: {e}"
        )


# ============================================================
# SYMBOLS
# ============================================================

def get_usdt_futures_symbols():

    data = _get_with_retry(
        f"{BINANCE_FAPI}/fapi/v1/exchangeInfo"
    )

    symbols = [

        s["symbol"]

        for s in data["symbols"]

        if (
            s["symbol"].endswith("USDT")
            and s.get("contractType") == "PERPETUAL"
            and s.get("status") == "TRADING"
        )
    ]

    return symbols


# ============================================================
# 24H VOLUME
# ============================================================

def get_24h_volumes():

    data = _get_with_retry(
        f"{BINANCE_FAPI}/fapi/v1/ticker/24hr"
    )

    return {
        item["symbol"]:
        float(item["quoteVolume"])
        for item in data
    }


# ============================================================
# BULK PRICE
# ============================================================

def get_all_prices():

    data = _get_with_retry(
        f"{BINANCE_FAPI}/fapi/v1/ticker/price"
    )

    return {
        item["symbol"]:
        float(item["price"])
        for item in data
    }


# ============================================================
# 15M KLINE
# ============================================================

def get_15m_klines(symbol):

    return _get_with_retry(
        f"{BINANCE_FAPI}/fapi/v1/klines",
        params={
            "symbol": symbol,
            "interval": "15m",
            "limit": MAJOR_LOOKBACK + 5
        }
    )


# ============================================================
# PIVOT LOW
# ============================================================

def is_pivot_low(candles, index):

    if index - PIVOT_LEFT < 0:
        return False

    if index + PIVOT_RIGHT >= len(candles):
        return False

    low = float(
        candles[index][3]
    )

    for i in range(
        index - PIVOT_LEFT,
        index + PIVOT_RIGHT + 1
    ):

        if i == index:
            continue

        if float(candles[i][3]) <= low:
            return False

    return True


# ============================================================
# PIVOT HIGH
# ============================================================

def is_pivot_high(candles, index):

    if index - PIVOT_LEFT < 0:
        return False

    if index + PIVOT_RIGHT >= len(candles):
        return False

    high = float(
        candles[index][2]
    )

    for i in range(
        index - PIVOT_LEFT,
        index + PIVOT_RIGHT + 1
    ):

        if i == index:
            continue

        if float(candles[i][2]) >= high:
            return False

    return True


# ============================================================
# CLUSTER LEVELS
# ============================================================

def cluster_levels(levels):

    if not levels:
        return []

    levels = sorted(levels)

    clusters = []

    current = [
        levels[0]
    ]

    for price in levels[1:]:

        average = (
            sum(current)
            / len(current)
        )

        distance = (
            abs(price - average)
            / average
        )

        if distance <= MAJOR_TOLERANCE:

            current.append(price)

        else:

            clusters.append(
                current
            )

            current = [
                price
            ]

    clusters.append(current)

    return clusters


# ============================================================
# MAJOR LEVEL CALCULATION
# ============================================================

def calculate_major_levels(candles):

    # Açık 15m mum kullanılmaz.
    closed = candles[:-1]

    if len(closed) < 30:
        return None, None

    support_pivots = []
    resistance_pivots = []

    end = (
        len(closed)
        - PIVOT_RIGHT
    )

    for i in range(
        PIVOT_LEFT,
        end
    ):

        if is_pivot_low(
            closed,
            i
        ):

            support_pivots.append(
                float(
                    closed[i][3]
                )
            )

        if is_pivot_high(
            closed,
            i
        ):

            resistance_pivots.append(
                float(
                    closed[i][2]
                )
            )

    support_clusters = cluster_levels(
        support_pivots
    )

    resistance_clusters = cluster_levels(
        resistance_pivots
    )

    # --------------------------------------------------------
    # MAJOR SUPPORT
    # --------------------------------------------------------

    valid_supports = [

        sum(cluster)
        / len(cluster)

        for cluster
        in support_clusters

        if len(cluster)
        >= MAJOR_TOUCHES
    ]

    # --------------------------------------------------------
    # MAJOR RESISTANCE
    # --------------------------------------------------------

    valid_resistances = [

        sum(cluster)
        / len(cluster)

        for cluster
        in resistance_clusters

        if len(cluster)
        >= MAJOR_TOUCHES
    ]

    if valid_supports:

        # En güncel / yüksek destek
        support = max(
            valid_supports
        )

    else:

        support = None

    if valid_resistances:

        # En güncel / düşük direnç
        resistance = min(
            valid_resistances
        )

    else:

        resistance = None

    return support, resistance


# ============================================================
# REFRESH LEVELS
# ============================================================

def refresh_major_levels(
    candidates
):

    now = time.time()

    updated = 0
    failed = 0

    for symbol in candidates:

        last = last_kline_refresh.get(
            symbol,
            0
        )

        if (
            now - last
            < KLINE_REFRESH_SEC
        ):

            continue

        try:

            candles = get_15m_klines(
                symbol
            )

            support, resistance = (
                calculate_major_levels(
                    candles
                )
            )

            major_levels[symbol] = {

                "support": support,

                "resistance": resistance,

                "updated": now
            }

            last_kline_refresh[symbol] = now

            updated += 1

        except Exception as e:

            failed += 1

            log.warning(
                f"{symbol} level error: {e}"
            )

    log.info(
        f"Major levels | "
        f"updated={updated} "
        f"failed={failed}"
    )


# ============================================================
# SIGNAL CHECK
# ============================================================

def check_signal(
    symbol,
    price,
    volume,
    window
):

    signals = []

    levels = major_levels.get(
        symbol
    )

    if not levels:
        return signals

    support = levels.get(
        "support"
    )

    resistance = levels.get(
        "resistance"
    )

    # ========================================================
    # SUPPORT BREAKDOWN
    # ========================================================

    if support and support > 0:

        # Fiyat support'un ne kadar altında?
        breakdown_pct = (
            (
                support - price
            )
            / support
        ) * 100

        # ÖNEMLİ:
        # Artık previous_price >= support
        # şartı YOK.
        #
        # Böylece bot ilk crossing'i kaçırsa bile
        # fiyat %5 seviyesine ulaştığında alarm verir.

        if (
            price < support
            and breakdown_pct
            >= BREAKOUT_THRESHOLD
        ):

            if (
                alerted_support.get(symbol)
                != window
            ):

                signals.append({

                    "type":
                    "SUPPORT_BREAKDOWN",

                    "symbol":
                    symbol,

                    "level":
                    support,

                    "price":
                    price,

                    "change_pct":
                    -breakdown_pct,

                    "volume":
                    volume,

                    "window":
                    window
                })

                alerted_support[
                    symbol
                ] = window

    # ========================================================
    # RESISTANCE BREAKOUT
    # ========================================================

    if resistance and resistance > 0:

        # Fiyat resistance'ın ne kadar üstünde?
        breakout_pct = (
            (
                price - resistance
            )
            / resistance
        ) * 100

        if (
            price > resistance
            and breakout_pct
            >= BREAKOUT_THRESHOLD
        ):

            if (
                alerted_resistance.get(symbol)
                != window
            ):

                signals.append({

                    "type":
                    "RESISTANCE_BREAKOUT",

                    "symbol":
                    symbol,

                    "level":
                    resistance,

                    "price":
                    price,

                    "change_pct":
                    breakout_pct,

                    "volume":
                    volume,

                    "window":
                    window
                })

                alerted_resistance[
                    symbol
                ] = window

    previous_prices[
        symbol
    ] = price

    return signals


# ============================================================
# ALERT FORMAT
# ============================================================

def format_alert(hit):

    if (
        hit["type"]
        == "SUPPORT_BREAKDOWN"
    ):

        title = (
            "🔴 MAJOR SUPPORT BREAKDOWN"
        )

        movement = (
            f"Support'tan uzaklık: "
            f"{hit['change_pct']:.2f}%"
        )

    else:

        title = (
            "🟢 MAJOR RESISTANCE BREAKOUT"
        )

        movement = (
            f"Resistance'tan uzaklık: "
            f"+{hit['change_pct']:.2f}%"
        )

    return (
        f"{title}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"Coin: {hit['symbol']}\n"
        f"Major Level: {hit['level']}\n"
        f"Fiyat: {hit['price']}\n"
        f"{movement}\n"
        f"24h Hacim: "
        f"${hit['volume']:,.0f}\n"
        f"Timeframe: 15m\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"{datetime.now(timezone.utc).strftime('%H:%M:%S UTC')}"
    )


# ============================================================
# SCAN
# ============================================================

def scan_once():

    # --------------------------------------------------------
    # SYMBOLS
    # --------------------------------------------------------

    symbols = get_usdt_futures_symbols()

    if not symbols:

        log.error(
            "Symbol listesi alınamadı."
        )

        return []

    # --------------------------------------------------------
    # VOLUME
    # --------------------------------------------------------

    volumes = get_24h_volumes()

    candidates = [

        symbol

        for symbol in symbols

        if volumes.get(
            symbol,
            0
        ) >= VOLUME_USDT_MIN
    ]

    log.info(
        f"Futures: {len(symbols)} | "
        f"24h >= ${VOLUME_USDT_MIN:,.0f}: "
        f"{len(candidates)}"
    )

    if not candidates:
        return []

    # --------------------------------------------------------
    # MAJOR LEVELS
    # --------------------------------------------------------

    refresh_major_levels(
        candidates
    )

    # --------------------------------------------------------
    # CURRENT PRICES
    # --------------------------------------------------------

    prices = get_all_prices()

    if not prices:

        log.error(
            "Fiyat verisi alınamadı."
        )

        return []

    # --------------------------------------------------------
    # CURRENT 15M WINDOW
    # --------------------------------------------------------

    now_ms = int(
        time.time() * 1000
    )

    current_window = (
        now_ms // WINDOW_MS
    ) * WINDOW_MS

    hits = []

    for symbol in candidates:

        price = prices.get(
            symbol
        )

        if price is None:
            continue

        signals = check_signal(

            symbol=symbol,

            price=price,

            volume=volumes.get(
                symbol,
                0
            ),

            window=current_window
        )

        hits.extend(
            signals
        )

    return hits


# ============================================================
# CLEAN OLD MEMORY
# ============================================================

def cleanup_memory():

    now_ms = int(
        time.time() * 1000
    )

    current_window = (
        now_ms // WINDOW_MS
    ) * WINDOW_MS

    # 1 saatten eski alert kayıtlarını temizle
    cutoff = (
        current_window
        - (4 * WINDOW_MS)
    )

    for storage in [
        alerted_support,
        alerted_resistance
    ]:

        old_symbols = [

            symbol

            for symbol, window
            in storage.items()

            if window < cutoff
        ]

        for symbol in old_symbols:

            del storage[
                symbol
            ]


# ============================================================
# MAIN
# ============================================================

def main():

    log.info(
        "=========================================="
    )

    log.info(
        "MAJOR SUPPORT / RESISTANCE SCANNER"
    )

    log.info(
        "=========================================="
    )

    log.info(
        f"24h Volume >= "
        f"${VOLUME_USDT_MIN:,.0f}"
    )

    log.info(
        f"Breakout / Breakdown >= "
        f"{BREAKOUT_THRESHOLD}%"
    )

    log.info(
        f"Major Lookback = "
        f"{MAJOR_LOOKBACK} x 15m"
    )

    log.info(
        f"Major Touches = "
        f"{MAJOR_TOUCHES}"
    )

    log.info(
        f"Tolerance = "
        f"{MAJOR_TOLERANCE * 100:.2f}%"
    )

    log.info(
        f"Scan interval = "
        f"{SCAN_INTERVAL_SEC}s"
    )

    log.info(
        "=========================================="
    )

    if (
        not TELEGRAM_TOKEN
        or not TELEGRAM_CHAT_ID
    ):

        log.warning(
            "TELEGRAM_TOKEN veya "
            "TELEGRAM_CHAT_ID eksik."
        )

    else:

        send_telegram(
            "✅ Major Support / Resistance "
            "Scanner başladı.\n\n"
            f"24h Volume >= "
            f"${VOLUME_USDT_MIN:,.0f}\n"
            f"Breakout / Breakdown >= "
            f"{BREAKOUT_THRESHOLD}%\n"
            f"Timeframe: 15m\n"
            f"Support + Resistance aynı scanner."
        )

    # ========================================================
    # LOOP
    # ========================================================

    while True:

        started = time.time()

        try:

            hits = scan_once()

            for hit in hits:

                message = format_alert(
                    hit
                )

                log.info(
                    "\n" + message
                )

                send_telegram(
                    message
                )

            cleanup_memory()

        except Exception as e:

            log.exception(
                f"Tarama döngüsü hatası: {e}"
            )

        elapsed = (
            time.time() - started
        )

        sleep_time = max(
            1,
            SCAN_INTERVAL_SEC - elapsed
        )

        time.sleep(
            sleep_time
        )


# ============================================================
# START
# ============================================================

if __name__ == "__main__":
    main()
