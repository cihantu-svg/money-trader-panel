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
# ENV SETTINGS
# ============================================================

# Minimum 24h USDT quote volume
VOLUME_USDT_MIN = float(
    os.getenv("VOLUME_USDT_MIN", "1000000")
)

# Breakout / breakdown minimum percentage
BREAKOUT_THRESHOLD = float(
    os.getenv("BREAKOUT_THRESHOLD", "5")
)

# Main scanner interval
SCAN_INTERVAL_SEC = int(
    os.getenv(
        "SCAN_INTERVAL_SEC",
        os.getenv("CHECK_INTERVAL_SEC", "10")
    )
)

# How often major levels are recalculated
LEVEL_REFRESH_SEC = int(
    os.getenv("LEVEL_REFRESH_SEC", "60")
)

# Number of closed 15m candles used
# 96 = 24 hours
MAJOR_LOOKBACK = int(
    os.getenv("MAJOR_LOOKBACK", "96")
)

# Minimum number of touches required
MAJOR_TOUCHES = int(
    os.getenv("MAJOR_TOUCHES", "2")
)

# 0.003 = 0.3%
MAJOR_TOLERANCE = float(
    os.getenv("MAJOR_TOLERANCE", "0.003")
)

# Number of recent candles used to detect pivot
PIVOT_LEFT = int(
    os.getenv("PIVOT_LEFT", "2")
)

PIVOT_RIGHT = int(
    os.getenv("PIVOT_RIGHT", "2")
)

# How often 15m klines are refreshed
KLINE_REFRESH_SEC = int(
    os.getenv("KLINE_REFRESH_SEC", "60")
)

# Telegram
TELEGRAM_TOKEN = os.getenv(
    "TELEGRAM_TOKEN",
    ""
)

TELEGRAM_CHAT_ID = os.getenv(
    "TELEGRAM_CHAT_ID",
    ""
)

# Binance
BINANCE_FAPI = "https://fapi.binance.com"

WINDOW_MS = 15 * 60 * 1000

MAX_RETRIES = 3
RETRY_BACKOFF_BASE = 0.5


# ============================================================
# HTTP SESSION
# ============================================================

session = requests.Session()

_adapter = requests.adapters.HTTPAdapter(
    pool_connections=20,
    pool_maxsize=20
)

session.mount("https://", _adapter)


# ============================================================
# MEMORY
# ============================================================

# symbol -> {
#   "support": float,
#   "resistance": float,
#   "updated": timestamp
# }
major_levels = {}

# symbol -> last price
previous_prices = {}

# symbol -> last 15m window where alert was sent
alerted_support = {}
alerted_resistance = {}

# symbol -> last kline refresh time
last_kline_refresh = {}


# ============================================================
# HTTP
# ============================================================

def _get_with_retry(url, params=None, timeout=15):

    attempt = 0

    while True:

        try:

            response = session.get(
                url,
                params=params,
                timeout=timeout
            )

        except requests.exceptions.RequestException as e:

            attempt += 1

            if attempt > MAX_RETRIES:
                raise e

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

        if response.status_code == 200:
            return response.json()

        # ----------------------------------------------------
        # 418 BAN
        # ----------------------------------------------------

        if response.status_code == 418:

            retry_after = response.headers.get(
                "Retry-After"
            )

            try:
                wait = float(retry_after) if retry_after else 60.0
            except ValueError:
                wait = 60.0

            wait += 2

            log.error(
                f"HTTP 418 Binance IP BAN. "
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
        # 429 RATE LIMIT
        # ----------------------------------------------------

        if response.status_code == 429:

            retry_after = response.headers.get(
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
                f"HTTP 429 | {wait:.1f}s bekleniyor | "
                f"deneme {attempt}"
            )

            time.sleep(wait)

            if attempt > MAX_RETRIES:
                raise Exception(
                    "HTTP 429 - retry limiti aşıldı"
                )

            continue

        # ----------------------------------------------------
        # OTHER ERRORS
        # ----------------------------------------------------

        attempt += 1

        wait = RETRY_BACKOFF_BASE * (
            2 ** attempt
        )

        log.warning(
            f"HTTP {response.status_code} | "
            f"{wait:.1f}s sonra tekrar"
        )

        time.sleep(wait)

        if attempt > MAX_RETRIES:

            raise Exception(
                f"HTTP {response.status_code}"
            )


# ============================================================
# TELEGRAM
# ============================================================

def send_telegram(msg):

    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:

        log.warning(
            "[TELEGRAM DEVRE DIŞI] "
            "TOKEN veya CHAT_ID yok"
        )

        return

    url = (
        f"https://api.telegram.org/"
        f"bot{TELEGRAM_TOKEN}/sendMessage"
    )

    try:

        response = session.post(
            url,
            data={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": msg
            },
            timeout=10
        )

        if response.status_code != 200:

            log.error(
                f"Telegram hata: "
                f"HTTP {response.status_code}"
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
        f"{BINANCE_FAPI}/fapi/v1/exchangeInfo",
        timeout=15
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
        f"{BINANCE_FAPI}/fapi/v1/ticker/24hr",
        timeout=15
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
        f"{BINANCE_FAPI}/fapi/v1/ticker/price",
        timeout=15
    )

    return {
        item["symbol"]:
        float(item["price"])

        for item in data
    }


# ============================================================
# 15M KLINES
# ============================================================

def get_15m_klines(symbol):

    data = _get_with_retry(
        f"{BINANCE_FAPI}/fapi/v1/klines",
        params={
            "symbol": symbol,
            "interval": "15m",
            "limit": MAJOR_LOOKBACK + 5
        },
        timeout=15
    )

    return data


# ============================================================
# PIVOT DETECTION
# ============================================================

def is_pivot_low(
    candles,
    index
):

    left = PIVOT_LEFT
    right = PIVOT_RIGHT

    if index - left < 0:
        return False

    if index + right >= len(candles):
        return False

    low = float(candles[index][3])

    for i in range(
        index - left,
        index + right + 1
    ):

        if i == index:
            continue

        if float(candles[i][3]) <= low:
            return False

    return True


def is_pivot_high(
    candles,
    index
):

    left = PIVOT_LEFT
    right = PIVOT_RIGHT

    if index - left < 0:
        return False

    if index + right >= len(candles):
        return False

    high = float(candles[index][2])

    for i in range(
        index - left,
        index + right + 1
    ):

        if i == index:
            continue

        if float(candles[i][2]) >= high:
            return False

    return True


# ============================================================
# CLUSTER PIVOTS
# ============================================================

def cluster_levels(levels):

    if not levels:
        return []

    levels = sorted(levels)

    clusters = []

    current = [levels[0]]

    for price in levels[1:]:

        average = sum(current) / len(current)

        if (
            abs(price - average) / average
            <= MAJOR_TOLERANCE
        ):

            current.append(price)

        else:

            clusters.append(current)

            current = [price]

    clusters.append(current)

    return clusters


# ============================================================
# MAJOR LEVEL CALCULATION
# ============================================================

def calculate_major_levels(candles):

    # Son mum açık olduğu için onu kullanmıyoruz.
    closed = candles[:-1]

    if len(closed) < 20:
        return None, None

    support_pivots = []
    resistance_pivots = []

    start = max(
        PIVOT_LEFT,
        0
    )

    end = len(closed) - PIVOT_RIGHT

    for i in range(start, end):

        if is_pivot_low(
            closed,
            i
        ):

            support_pivots.append(
                float(closed[i][3])
            )

        if is_pivot_high(
            closed,
            i
        ):

            resistance_pivots.append(
                float(closed[i][2])
            )

    support_clusters = cluster_levels(
        support_pivots
    )

    resistance_clusters = cluster_levels(
        resistance_pivots
    )

    # --------------------------------------------------------
    # Only levels with enough touches
    # --------------------------------------------------------

    support_candidates = [

        sum(cluster) / len(cluster)

        for cluster in support_clusters

        if len(cluster) >= MAJOR_TOUCHES
    ]

    resistance_candidates = [

        sum(cluster) / len(cluster)

        for cluster in resistance_clusters

        if len(cluster) >= MAJOR_TOUCHES
    ]

    if not support_candidates:
        support = None
    else:
        support = support_candidates[-1]

    if not resistance_candidates:
        resistance = None
    else:
        resistance = resistance_candidates[0]

    return support, resistance


# ============================================================
# REFRESH MAJOR LEVELS
# ============================================================

def refresh_major_levels(
    candidates
):

    now = time.time()

    updated = 0
    failed = 0

    for symbol in candidates:

        last_update = last_kline_refresh.get(
            symbol,
            0
        )

        if (
            now - last_update
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
                f"{symbol} major level alınamadı: {e}"
            )

    if updated or failed:

        log.info(
            f"Major level güncelleme | "
            f"güncellenen: {updated} | "
            f"hatalı: {failed}"
        )


# ============================================================
# BREAKOUT CHECK
# ============================================================

def check_signal(
    symbol,
    price,
    volume,
    window
):

    result = []

    levels = major_levels.get(symbol)

    if not levels:
        return result

    support = levels.get("support")
    resistance = levels.get("resistance")

    previous_price = previous_prices.get(
        symbol
    )

    # İlk gözlemde crossing kontrolü yapma
    if previous_price is None:

        previous_prices[symbol] = price

        return result

    # ========================================================
    # MAJOR SUPPORT BREAKDOWN
    # ========================================================

    if support and support > 0:

        breakdown_pct = (
            (support - price)
            / support
            * 100
        )

        crossed_support = (
            previous_price >= support
            and price < support
        )

        # Hem crossing hem %5 şartı
        if (
            crossed_support
            and breakdown_pct >= BREAKOUT_THRESHOLD
        ):

            if alerted_support.get(symbol) != window:

                result.append({

                    "type": "SUPPORT_BREAKDOWN",

                    "symbol": symbol,

                    "level": support,

                    "price": price,

                    "change_pct": -breakdown_pct,

                    "volume": volume,

                    "window": window
                })

                alerted_support[symbol] = window

    # ========================================================
    # MAJOR RESISTANCE BREAKOUT
    # ========================================================

    if resistance and resistance > 0:

        breakout_pct = (
            (price - resistance)
            / resistance
            * 100
        )

        crossed_resistance = (
            previous_price <= resistance
            and price > resistance
        )

        if (
            crossed_resistance
            and breakout_pct >= BREAKOUT_THRESHOLD
        ):

            if alerted_resistance.get(symbol) != window:

                result.append({

                    "type": "RESISTANCE_BREAKOUT",

                    "symbol": symbol,

                    "level": resistance,

                    "price": price,

                    "change_pct": breakout_pct,

                    "volume": volume,

                    "window": window
                })

                alerted_resistance[symbol] = window

    previous_prices[symbol] = price

    return result


# ============================================================
# ALERT FORMAT
# ============================================================

def format_alert(hit):

    if hit["type"] == "SUPPORT_BREAKDOWN":

        title = "🔴 MAJOR SUPPORT BREAKDOWN"

        movement = (
            f"Support kırılımı: "
            f"{hit['change_pct']:.2f}%"
        )

    else:

        title = "🟢 MAJOR RESISTANCE BREAKOUT"

        movement = (
            f"Resistance kırılımı: "
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

    symbols = get_usdt_futures_symbols()

    if not symbols:

        log.error(
            "USDT futures sembolleri alınamadı."
        )

        return []

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
        f"Market: {len(symbols)} | "
        f"24h >= ${VOLUME_USDT_MIN:,.0f}: "
        f"{len(candidates)}"
    )

    if not candidates:
        return []

    # --------------------------------------------------------
    # Major levels
    # --------------------------------------------------------

    refresh_major_levels(
        candidates
    )

    # --------------------------------------------------------
    # Bulk current prices
    # --------------------------------------------------------

    prices = get_all_prices()

    if not prices:

        log.error(
            "Bulk fiyat verisi alınamadı."
        )

        return []

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
# MAIN
# ============================================================

def main():

    log.info(
        "=============================================="
    )

    log.info(
        "MAJOR SUPPORT / RESISTANCE SCANNER BAŞLADI"
    )

    log.info(
        f"24h Minimum Volume: "
        f"${VOLUME_USDT_MIN:,.0f}"
    )

    log.info(
        f"Breakout Threshold: "
        f"{BREAKOUT_THRESHOLD}%"
    )

    log.info(
        f"Major Lookback: "
        f"{MAJOR_LOOKBACK} x 15m"
    )

    log.info(
        f"Major Touches: "
        f"{MAJOR_TOUCHES}"
    )

    log.info(
        f"Major Tolerance: "
        f"{MAJOR_TOLERANCE * 100:.2f}%"
    )

    log.info(
        f"Scanner Interval: "
        f"{SCAN_INTERVAL_SEC}s"
    )

    log.info(
        f"Level Refresh: "
        f"{KLINE_REFRESH_SEC}s"
    )

    log.info(
        "=============================================="
    )

    if (
        not TELEGRAM_TOKEN
        or not TELEGRAM_CHAT_ID
    ):

        log.warning(
            "Telegram ENV eksik."
        )

    else:

        send_telegram(
            "✅ Major Support / Resistance Scanner başladı.\n\n"
            f"24h Volume >= ${VOLUME_USDT_MIN:,.0f}\n"
            f"Breakout/Breakdown >= %{BREAKOUT_THRESHOLD}\n"
            f"15m Major Levels\n"
            f"Support + Resistance aynı scanner."
        )

    while True:

        start = time.time()

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

        except Exception as e:

            log.exception(
                f"Tarama hatası: {e}"
            )

        elapsed = (
            time.time() - start
        )

        sleep_time = max(
            1,
            SCAN_INTERVAL_SEC - elapsed
        )

        time.sleep(
            sleep_time
        )


# ============================================================
# ENTRY
# ============================================================

if __name__ == "__main__":
    main()
