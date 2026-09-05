import os
import time
import logging
import requests
from collections import defaultdict

# =========================================================
# CONFIG
# =========================================================

BINANCE_BASE_URL = "https://fapi.binance.com"
TELEGRAM_URL = "https://api.telegram.org/bot{}/sendMessage"

VOLUME_USDT_MIN = float(os.getenv("VOLUME_USDT_MIN", "1000000"))

BREAKOUT_THRESHOLD = float(os.getenv("BREAKOUT_THRESHOLD", "5"))
MAX_BREAKOUT_DISTANCE = float(os.getenv("MAX_BREAKOUT_DISTANCE", "10"))

SCAN_INTERVAL_SEC = int(os.getenv("SCAN_INTERVAL_SEC", "10"))

SYMBOL_REFRESH_SEC = int(os.getenv("SYMBOL_REFRESH_SEC", "1800"))
VOLUME_REFRESH_SEC = int(os.getenv("VOLUME_REFRESH_SEC", "60"))
LEVEL_REFRESH_SEC = int(os.getenv("LEVEL_REFRESH_SEC", "60"))

MAJOR_LOOKBACK = int(os.getenv("MAJOR_LOOKBACK", "288"))
MAJOR_TOUCHES = int(os.getenv("MAJOR_TOUCHES", "2"))
MAJOR_TOLERANCE = float(os.getenv("MAJOR_TOLERANCE", "0.005"))

PIVOT_LEFT = int(os.getenv("PIVOT_LEFT", "2"))
PIVOT_RIGHT = int(os.getenv("PIVOT_RIGHT", "2"))

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")


# =========================================================
# LOGGING
# =========================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

logger = logging.getLogger("major-level-bot")


# =========================================================
# SESSION
# =========================================================

session = requests.Session()

session.headers.update({
    "User-Agent": "MajorLevelScanner/1.0"
})


# =========================================================
# CACHE
# =========================================================

symbols_cache = []
symbols_cache_time = 0

volume_cache = {}
volume_cache_time = 0

price_cache = {}

level_cache = {}

last_scan_time = 0


# =========================================================
# SIGNAL MEMORY
# =========================================================

alerted_support = {}
alerted_resistance = {}

# Track whether price was recently close to a level
support_armed = {}
resistance_armed = {}


# =========================================================
# HTTP
# =========================================================

def binance_get(endpoint, params=None, retries=3):

    url = BINANCE_BASE_URL + endpoint

    for attempt in range(retries):

        try:

            response = session.get(
                url,
                params=params,
                timeout=10
            )

            if response.status_code == 200:
                return response.json()

            if response.status_code in (418, 429):

                wait_time = min(
                    30,
                    2 ** attempt
                )

                logger.warning(
                    "Binance rate limit %s - waiting %ss",
                    response.status_code,
                    wait_time
                )

                time.sleep(wait_time)
                continue

            logger.warning(
                "Binance HTTP %s: %s",
                response.status_code,
                response.text[:200]
            )

        except requests.RequestException as e:

            logger.warning(
                "Binance request error: %s",
                e
            )

            time.sleep(2 ** attempt)

    return None


# =========================================================
# TELEGRAM
# =========================================================

def send_telegram(message):

    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:

        logger.error(
            "TELEGRAM_TOKEN veya TELEGRAM_CHAT_ID eksik."
        )

        return False

    url = TELEGRAM_URL.format(TELEGRAM_TOKEN)

    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message
    }

    try:

        response = requests.post(
            url,
            json=payload,
            timeout=10
        )

        if response.status_code == 200:
            return True

        logger.warning(
            "Telegram error: %s",
            response.text[:300]
        )

    except requests.RequestException as e:

        logger.warning(
            "Telegram request error: %s",
            e
        )

    return False


# =========================================================
# SYMBOLS
# =========================================================

def get_usdt_futures_symbols():

    global symbols_cache
    global symbols_cache_time

    now = time.time()

    if (
        symbols_cache
        and now - symbols_cache_time < SYMBOL_REFRESH_SEC
    ):
        return symbols_cache

    data = binance_get("/fapi/v1/exchangeInfo")

    if not data:
        return symbols_cache

    result = []

    for symbol in data.get("symbols", []):

        if (
            symbol.get("status") == "TRADING"
            and symbol.get("quoteAsset") == "USDT"
            and symbol.get("contractType") == "PERPETUAL"
        ):

            result.append(symbol["symbol"])

    symbols_cache = result
    symbols_cache_time = now

    logger.info(
        "USDT perpetual symbols: %s",
        len(result)
    )

    return result


# =========================================================
# 24H VOLUME
# =========================================================

def get_24h_volumes():

    global volume_cache
    global volume_cache_time

    now = time.time()

    if (
        volume_cache
        and now - volume_cache_time < VOLUME_REFRESH_SEC
    ):
        return volume_cache

    data = binance_get("/fapi/v1/ticker/24hr")

    if not data:
        return volume_cache

    result = {}

    for item in data:

        symbol = item.get("symbol")

        if not symbol:
            continue

        try:

            quote_volume = float(
                item.get("quoteVolume", 0)
            )

            result[symbol] = quote_volume

        except (TypeError, ValueError):

            continue

    volume_cache = result
    volume_cache_time = now

    return result


# =========================================================
# ALL PRICES
# =========================================================

def get_all_prices():

    data = binance_get("/fapi/v1/ticker/price")

    if not data:
        return {}

    result = {}

    for item in data:

        symbol = item.get("symbol")

        try:

            price = float(item.get("price", 0))

            if price > 0:
                result[symbol] = price

        except (TypeError, ValueError):

            continue

    return result


# =========================================================
# 15M KLINES
# =========================================================

def get_15m_klines(symbol):

    data = binance_get(
        "/fapi/v1/klines",
        params={
            "symbol": symbol,
            "interval": "15m",
            "limit": MAJOR_LOOKBACK + 10
        }
    )

    if not data:
        return []

    candles = []

    for candle in data:

        try:

            candles.append({
                "open_time": int(candle[0]),
                "open": float(candle[1]),
                "high": float(candle[2]),
                "low": float(candle[3]),
                "close": float(candle[4])
            })

        except (TypeError, ValueError, IndexError):

            continue

    return candles


# =========================================================
# PIVOTS
# =========================================================

def find_pivot_highs(candles):

    pivots = []

    start = PIVOT_LEFT
    end = len(candles) - PIVOT_RIGHT

    for i in range(start, end):

        current_high = candles[i]["high"]

        is_pivot = True

        for j in range(
            i - PIVOT_LEFT,
            i + PIVOT_RIGHT + 1
        ):

            if j == i:
                continue

            if candles[j]["high"] >= current_high:

                is_pivot = False
                break

        if is_pivot:
            pivots.append(current_high)

    return pivots


def find_pivot_lows(candles):

    pivots = []

    start = PIVOT_LEFT
    end = len(candles) - PIVOT_RIGHT

    for i in range(start, end):

        current_low = candles[i]["low"]

        is_pivot = True

        for j in range(
            i - PIVOT_LEFT,
            i + PIVOT_RIGHT + 1
        ):

            if j == i:
                continue

            if candles[j]["low"] <= current_low:

                is_pivot = False
                break

        if is_pivot:
            pivots.append(current_low)

    return pivots


# =========================================================
# LEVEL CLUSTERING
# =========================================================

def cluster_levels(levels):

    if not levels:
        return []

    levels = sorted(levels)

    clusters = []

    current_cluster = [levels[0]]

    for level in levels[1:]:

        average = sum(current_cluster) / len(current_cluster)

        distance = abs(level - average) / average

        if distance <= MAJOR_TOLERANCE:

            current_cluster.append(level)

        else:

            clusters.append(current_cluster)

            current_cluster = [level]

    clusters.append(current_cluster)

    result = []

    for cluster in clusters:

        if len(cluster) >= MAJOR_TOUCHES:

            average = sum(cluster) / len(cluster)

            result.append({
                "level": average,
                "touches": len(cluster)
            })

    return result


# =========================================================
# FIND MAJOR LEVELS
# =========================================================

def calculate_major_levels(candles, current_price):

    if len(candles) < 30:
        return None, None

    # Exclude current/open candle
    closed_candles = candles[:-1]

    pivot_highs = find_pivot_highs(
        closed_candles
    )

    pivot_lows = find_pivot_lows(
        closed_candles
    )

    resistance_clusters = cluster_levels(
        pivot_highs
    )

    support_clusters = cluster_levels(
        pivot_lows
    )

    # -----------------------------------------------------
    # IMPORTANT:
    # Support MUST be below current price
    # Resistance MUST be above current price
    # -----------------------------------------------------

    valid_supports = [
        x for x in support_clusters
        if x["level"] < current_price
    ]

    valid_resistances = [
        x for x in resistance_clusters
        if x["level"] > current_price
    ]

    # Nearest valid support below price
    support = None

    if valid_supports:

        nearest_support = max(
            valid_supports,
            key=lambda x: x["level"]
        )

        support = nearest_support["level"]

    # Nearest valid resistance above price
    resistance = None

    if valid_resistances:

        nearest_resistance = min(
            valid_resistances,
            key=lambda x: x["level"]
        )

        resistance = nearest_resistance["level"]

    return support, resistance


# =========================================================
# LEVEL CACHE
# =========================================================

def refresh_major_levels(
    symbol,
    current_price
):

    now = time.time()

    cached = level_cache.get(symbol)

    if cached:

        if now - cached["time"] < LEVEL_REFRESH_SEC:

            return (
                cached["support"],
                cached["resistance"]
            )

    candles = get_15m_klines(symbol)

    if not candles:

        return (
            cached["support"] if cached else None,
            cached["resistance"] if cached else None
        )

    support, resistance = calculate_major_levels(
        candles,
        current_price
    )

    level_cache[symbol] = {
        "time": now,
        "support": support,
        "resistance": resistance
    }

    return support, resistance


# =========================================================
# 15M WINDOW
# =========================================================

def get_current_15m_window():

    now_ms = int(time.time() * 1000)

    fifteen_minutes_ms = 15 * 60 * 1000

    return (
        now_ms // fifteen_minutes_ms
    )


# =========================================================
# FORMAT PRICE
# =========================================================

def format_price(price):

    if price >= 1000:
        return f"{price:.2f}"

    if price >= 1:
        return f"{price:.4f}"

    if price >= 0.1:
        return f"{price:.5f}"

    if price >= 0.01:
        return f"{price:.6f}"

    if price >= 0.001:
        return f"{price:.7f}"

    return f"{price:.10f}"


# =========================================================
# ALERT FORMAT
# =========================================================

def format_alert(
    symbol,
    direction,
    level,
    price,
    distance,
    volume
):

    if direction == "support":

        title = "🔴 MAJOR SUPPORT BREAKDOWN"

        distance_text = f"-{distance:.2f}%"

    else:

        title = "🟢 MAJOR RESISTANCE BREAKOUT"

        distance_text = f"+{distance:.2f}%"

    utc_time = time.strftime(
        "%H:%M:%S UTC",
        time.gmtime()
    )

    return f"""
{title}

━━━━━━━━━━━━━━━━━━

Coin: {symbol}

Major Level: {format_price(level)}

Fiyat: {format_price(price)}

Level'dan uzaklık: {distance_text}

24h Hacim: ${volume:,.0f}

Timeframe: 15m

━━━━━━━━━━━━━━━━━━

{utc_time}
""".strip()


# =========================================================
# CHECK SUPPORT
# =========================================================

def check_support_signal(
    symbol,
    price,
    support,
    volume,
    window
):

    if support is None:
        return

    # Price must actually be below support
    if price >= support:

        # Reset when price returns above level
        support_armed[symbol] = False

        return

    distance = (
        (support - price)
        / support
        * 100
    )

    # -----------------------------------------------------
    # Price has entered the "break zone".
    #
    # We arm the setup between 0% and 5%.
    # Then alert when it reaches 5%-10%.
    # -----------------------------------------------------

    if 0 <= distance < BREAKOUT_THRESHOLD:

        support_armed[symbol] = True

        return

    # Too far = stale move
    if distance > MAX_BREAKOUT_DISTANCE:

        support_armed[symbol] = False

        return

    # -----------------------------------------------------
    # Only trigger if:
    #
    # 1. price previously entered the break zone
    # 2. now reached >= 5%
    # -----------------------------------------------------

    if distance >= BREAKOUT_THRESHOLD:

        if not support_armed.get(symbol, False):

            return

        if alerted_support.get(symbol) == window:

            return

        message = format_alert(
            symbol,
            "support",
            support,
            price,
            distance,
            volume
        )

        if send_telegram(message):

            alerted_support[symbol] = window

            logger.info(
                "SUPPORT BREAKDOWN | %s | %.2f%%",
                symbol,
                distance
            )


# =========================================================
# CHECK RESISTANCE
# =========================================================

def check_resistance_signal(
    symbol,
    price,
    resistance,
    volume,
    window
):

    if resistance is None:
        return

    # Price must actually be above resistance
    if price <= resistance:

        # Reset when price returns below level
        resistance_armed[symbol] = False

        return

    distance = (
        (price - resistance)
        / resistance
        * 100
    )

    # Entered break zone
    if 0 <= distance < BREAKOUT_THRESHOLD:

        resistance_armed[symbol] = True

        return

    # Too far = stale move
    if distance > MAX_BREAKOUT_DISTANCE:

        resistance_armed[symbol] = False

        return

    # Actual breakout
    if distance >= BREAKOUT_THRESHOLD:

        if not resistance_armed.get(symbol, False):

            return

        if alerted_resistance.get(symbol) == window:

            return

        message = format_alert(
            symbol,
            "resistance",
            resistance,
            price,
            distance,
            volume
        )

        if send_telegram(message):

            alerted_resistance[symbol] = window

            logger.info(
                "RESISTANCE BREAKOUT | %s | %.2f%%",
                symbol,
                distance
            )


# =========================================================
# CLEANUP
# =========================================================

def cleanup_memory():

    current_window = get_current_15m_window()

    old_limit = current_window - 4

    for symbol in list(
        alerted_support.keys()
    ):

        if alerted_support[symbol] < old_limit:

            del alerted_support[symbol]

    for symbol in list(
        alerted_resistance.keys()
    ):

        if alerted_resistance[symbol] < old_limit:

            del alerted_resistance[symbol]


# =========================================================
# MAIN SCAN
# =========================================================

def scan():

    symbols = get_usdt_futures_symbols()

    if not symbols:
        return

    volumes = get_24h_volumes()

    prices = get_all_prices()

    if not prices:
        return

    window = get_current_15m_window()

    eligible = []

    for symbol in symbols:

        volume = volumes.get(symbol, 0)

        if volume < VOLUME_USDT_MIN:
            continue

        price = prices.get(symbol)

        if not price or price <= 0:
            continue

        eligible.append(
            (symbol, price, volume)
        )

    logger.info(
        "Eligible coins: %s",
        len(eligible)
    )

    # -----------------------------------------------------
    # Analyze eligible symbols
    # -----------------------------------------------------

    for symbol, price, volume in eligible:

        try:

            support, resistance = refresh_major_levels(
                symbol,
                price
            )

            check_support_signal(
                symbol,
                price,
                support,
                volume,
                window
            )

            check_resistance_signal(
                symbol,
                price,
                resistance,
                volume,
                window
            )

        except Exception as e:

            logger.exception(
                "Error processing %s: %s",
                symbol,
                e
            )

    cleanup_memory()


# =========================================================
# START
# =========================================================

def main():

    logger.info(
        "=========================================="
    )

    logger.info(
        "MAJOR SUPPORT / RESISTANCE BOT STARTED"
    )

    logger.info(
        "Volume minimum: $%s",
        f"{VOLUME_USDT_MIN:,.0f}"
    )

    logger.info(
        "Breakout threshold: %.2f%%",
        BREAKOUT_THRESHOLD
    )

    logger.info(
        "Maximum breakout distance: %.2f%%",
        MAX_BREAKOUT_DISTANCE
    )

    logger.info(
        "Scan interval: %ss",
        SCAN_INTERVAL_SEC
    )

    logger.info(
        "Major lookback: %s candles",
        MAJOR_LOOKBACK
    )

    logger.info(
        "=========================================="
    )

    while True:

        start = time.time()

        try:

            scan()

        except Exception as e:

            logger.exception(
                "Main scan error: %s",
                e
            )

        elapsed = time.time() - start

        sleep_time = max(
            1,
            SCAN_INTERVAL_SEC - elapsed
        )

        time.sleep(sleep_time)


if __name__ == "__main__":
    main()
