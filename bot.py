import os
import time
import logging
import requests


# =========================================================
# CONFIG
# =========================================================

BINANCE_BASE_URL = "https://fapi.binance.com"

VOLUME_USDT_MIN = float(
    os.getenv("VOLUME_USDT_MIN", "1000000")
)

BREAKOUT_THRESHOLD = float(
    os.getenv("BREAKOUT_THRESHOLD", "5")
)

MAX_BREAKOUT_DISTANCE = float(
    os.getenv("MAX_BREAKOUT_DISTANCE", "10")
)

SCAN_INTERVAL_SEC = int(
    os.getenv("SCAN_INTERVAL_SEC", "10")
)

SYMBOL_REFRESH_SEC = int(
    os.getenv("SYMBOL_REFRESH_SEC", "1800")
)

VOLUME_REFRESH_SEC = int(
    os.getenv("VOLUME_REFRESH_SEC", "60")
)

LEVEL_REFRESH_SEC = int(
    os.getenv("LEVEL_REFRESH_SEC", "60")
)

MAJOR_LOOKBACK = int(
    os.getenv("MAJOR_LOOKBACK", "288")
)

MAJOR_TOUCHES = int(
    os.getenv("MAJOR_TOUCHES", "2")
)

MAJOR_TOLERANCE = float(
    os.getenv("MAJOR_TOLERANCE", "0.005")
)

PIVOT_LEFT = int(
    os.getenv("PIVOT_LEFT", "2")
)

PIVOT_RIGHT = int(
    os.getenv("PIVOT_RIGHT", "2")
)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")


# =========================================================
# LOGGING
# =========================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

logger = logging.getLogger("MAJOR_LEVEL_BOT")


# =========================================================
# HTTP SESSION
# =========================================================

session = requests.Session()

session.headers.update({
    "User-Agent": "MajorLevelScanner/2.0"
})


# =========================================================
# CACHE
# =========================================================

symbols_cache = []
symbols_cache_time = 0

volume_cache = {}
volume_cache_time = 0

level_cache = {}


# =========================================================
# STATE
# =========================================================

# Previous price for detecting REAL crossing
previous_prices = {}

# Current valid level state
previous_levels = {}

# Whether the price actually crossed a level
support_break_started = {}
resistance_break_started = {}

# Alert memory
alerted_support = {}
alerted_resistance = {}


# =========================================================
# BINANCE GET
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

                wait = min(
                    30,
                    2 ** attempt
                )

                logger.warning(
                    "Binance rate limit %s - waiting %ss",
                    response.status_code,
                    wait
                )

                time.sleep(wait)
                continue

            logger.warning(
                "Binance HTTP %s",
                response.status_code
            )

        except requests.RequestException as e:

            logger.warning(
                "Binance error: %s",
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
            "Telegram ENV missing"
        )

        return False

    url = (
        f"https://api.telegram.org/"
        f"bot{TELEGRAM_TOKEN}/sendMessage"
    )

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
            response.text[:200]
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

def get_symbols():

    global symbols_cache
    global symbols_cache_time

    now = time.time()

    if (
        symbols_cache
        and now - symbols_cache_time
        < SYMBOL_REFRESH_SEC
    ):
        return symbols_cache

    data = binance_get(
        "/fapi/v1/exchangeInfo"
    )

    if not data:
        return symbols_cache

    result = []

    for item in data.get("symbols", []):

        if (
            item.get("status") == "TRADING"
            and item.get("quoteAsset") == "USDT"
            and item.get("contractType") == "PERPETUAL"
        ):

            result.append(
                item["symbol"]
            )

    symbols_cache = result
    symbols_cache_time = now

    logger.info(
        "Symbols loaded: %s",
        len(result)
    )

    return result


# =========================================================
# VOLUME
# =========================================================

def get_volumes():

    global volume_cache
    global volume_cache_time

    now = time.time()

    if (
        volume_cache
        and now - volume_cache_time
        < VOLUME_REFRESH_SEC
    ):
        return volume_cache

    data = binance_get(
        "/fapi/v1/ticker/24hr"
    )

    if not data:
        return volume_cache

    result = {}

    for item in data:

        symbol = item.get("symbol")

        try:

            volume = float(
                item.get(
                    "quoteVolume",
                    0
                )
            )

            result[symbol] = volume

        except (TypeError, ValueError):

            continue

    volume_cache = result
    volume_cache_time = now

    return result


# =========================================================
# PRICES
# =========================================================

def get_prices():

    data = binance_get(
        "/fapi/v1/ticker/price"
    )

    if not data:
        return {}

    result = {}

    for item in data:

        try:

            result[item["symbol"]] = float(
                item["price"]
            )

        except (KeyError, TypeError, ValueError):

            continue

    return result


# =========================================================
# KLINES
# =========================================================

def get_klines(symbol):

    data = binance_get(
        "/fapi/v1/klines",
        {
            "symbol": symbol,
            "interval": "15m",
            "limit": MAJOR_LOOKBACK + 10
        }
    )

    if not data:
        return []

    candles = []

    for c in data:

        try:

            candles.append({
                "time": int(c[0]),
                "open": float(c[1]),
                "high": float(c[2]),
                "low": float(c[3]),
                "close": float(c[4])
            })

        except (
            IndexError,
            TypeError,
            ValueError
        ):

            continue

    return candles


# =========================================================
# PIVOT HIGH
# =========================================================

def pivot_highs(candles):

    result = []

    for i in range(
        PIVOT_LEFT,
        len(candles) - PIVOT_RIGHT
    ):

        value = candles[i]["high"]

        valid = True

        for j in range(
            i - PIVOT_LEFT,
            i + PIVOT_RIGHT + 1
        ):

            if j == i:
                continue

            if candles[j]["high"] >= value:

                valid = False
                break

        if valid:
            result.append(value)

    return result


# =========================================================
# PIVOT LOW
# =========================================================

def pivot_lows(candles):

    result = []

    for i in range(
        PIVOT_LEFT,
        len(candles) - PIVOT_RIGHT
    ):

        value = candles[i]["low"]

        valid = True

        for j in range(
            i - PIVOT_LEFT,
            i + PIVOT_RIGHT + 1
        ):

            if j == i:
                continue

            if candles[j]["low"] <= value:

                valid = False
                break

        if valid:
            result.append(value)

    return result


# =========================================================
# CLUSTER LEVELS
# =========================================================

def cluster_levels(levels):

    if not levels:
        return []

    levels = sorted(levels)

    clusters = []

    current = [levels[0]]

    for level in levels[1:]:

        average = sum(current) / len(current)

        distance = abs(
            level - average
        ) / average

        if distance <= MAJOR_TOLERANCE:

            current.append(level)

        else:

            clusters.append(current)

            current = [level]

    clusters.append(current)

    result = []

    for cluster in clusters:

        if len(cluster) >= MAJOR_TOUCHES:

            result.append({
                "level":
                    sum(cluster)
                    / len(cluster),

                "touches":
                    len(cluster)
            })

    return result


# =========================================================
# CALCULATE LEVELS
# =========================================================

def calculate_levels(
    candles,
    current_price
):

    if len(candles) < 30:
        return None, None

    # IMPORTANT:
    # Do NOT use open 15m candle
    candles = candles[:-1]

    highs = pivot_highs(candles)
    lows = pivot_lows(candles)

    resistance_clusters = cluster_levels(
        highs
    )

    support_clusters = cluster_levels(
        lows
    )

    # -----------------------------------------------------
    # ONLY SUPPORT BELOW PRICE
    # -----------------------------------------------------

    supports = [
        x["level"]
        for x in support_clusters
        if x["level"] < current_price
    ]

    # -----------------------------------------------------
    # ONLY RESISTANCE ABOVE PRICE
    # -----------------------------------------------------

    resistances = [
        x["level"]
        for x in resistance_clusters
        if x["level"] > current_price
    ]

    support = (
        max(supports)
        if supports
        else None
    )

    resistance = (
        min(resistances)
        if resistances
        else None
    )

    return support, resistance


# =========================================================
# GET LEVELS
# =========================================================

def get_levels(
    symbol,
    price,
    force=False
):

    now = time.time()

    cached = level_cache.get(
        symbol
    )

    # -----------------------------------------------------
    # FIRST IMPORTANT SAFETY CHECK
    #
    # Cached support MUST remain BELOW price
    # Cached resistance MUST remain ABOVE price
    #
    # If not -> immediately refresh.
    # -----------------------------------------------------

    if cached and not force:

        support = cached["support"]
        resistance = cached["resistance"]

        cache_valid = (
            support is None
            or support < price
        ) and (
            resistance is None
            or resistance > price
        )

        if (
            cache_valid
            and now - cached["time"]
            < LEVEL_REFRESH_SEC
        ):

            return support, resistance

    # -----------------------------------------------------
    # Refresh
    # -----------------------------------------------------

    candles = get_klines(symbol)

    if not candles:

        if cached:

            return (
                cached["support"],
                cached["resistance"]
            )

        return None, None

    support, resistance = calculate_levels(
        candles,
        price
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

def current_window():

    return int(
        time.time() // (15 * 60)
    )


# =========================================================
# PRICE FORMAT
# =========================================================

def fmt_price(price):

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
# ALERT
# =========================================================

def alert_message(
    symbol,
    direction,
    level,
    price,
    distance,
    volume
):

    if direction == "support":

        title = (
            "🔴 MAJOR SUPPORT BREAKDOWN"
        )

        distance_text = (
            f"-{distance:.2f}%"
        )

        level_name = "Support'tan uzaklık"

    else:

        title = (
            "🟢 MAJOR RESISTANCE BREAKOUT"
        )

        distance_text = (
            f"+{distance:.2f}%"
        )

        level_name = "Resistance'tan uzaklık"

    utc = time.strftime(
        "%H:%M:%S UTC",
        time.gmtime()
    )

    return f"""
{title}

━━━━━━━━━━━━━━━━━━

Coin: {symbol}

Major Level: {fmt_price(level)}

Fiyat: {fmt_price(price)}

{level_name}: {distance_text}

24h Hacim: ${volume:,.0f}

Timeframe: 15m

━━━━━━━━━━━━━━━━━━

{utc}
""".strip()


# =========================================================
# CHECK BREAKOUT
# =========================================================

def check_symbol(
    symbol,
    price,
    volume
):

    window = current_window()

    previous_price = previous_prices.get(
        symbol
    )

    # First observation:
    # NEVER send an alert.
    if previous_price is None:

        previous_prices[symbol] = price

        return

    # -----------------------------------------------------
    # Get valid levels
    # -----------------------------------------------------

    support, resistance = get_levels(
        symbol,
        price
    )

    # -----------------------------------------------------
    # SAFETY:
    #
    # If support is above current price,
    # refresh it immediately.
    #
    # If resistance is below current price,
    # refresh immediately.
    # -----------------------------------------------------

    if (
        support is not None
        and support >= price
    ):

        support, resistance = get_levels(
            symbol,
            price,
            force=True
        )

    if (
        resistance is not None
        and resistance <= price
    ):

        support, resistance = get_levels(
            symbol,
            price,
            force=True
        )

    # =====================================================
    # SUPPORT BREAKDOWN
    # =====================================================

    if support is not None:

        # Previous price was ABOVE support
        # Current price is BELOW support
        #
        # This means REAL crossing happened.

        crossed_support = (
            previous_price >= support
            and price < support
        )

        if crossed_support:

            support_break_started[symbol] = {
                "window": window,
                "level": support
            }

        started = (
            support_break_started.get(
                symbol
            )
        )

        if started:

            level = started["level"]

            # Distance from original broken level
            distance = (
                (level - price)
                / level
                * 100
            )

            # ------------------------------------------------
            # Only 5% - 10%
            # ------------------------------------------------

            if (
                BREAKOUT_THRESHOLD
                <= distance
                <= MAX_BREAKOUT_DISTANCE
            ):

                if (
                    alerted_support.get(symbol)
                    != window
                ):

                    message = alert_message(
                        symbol,
                        "support",
                        level,
                        price,
                        distance,
                        volume
                    )

                    if send_telegram(message):

                        alerted_support[symbol] = (
                            window
                        )

                        logger.info(
                            "SUPPORT BREAKDOWN %s %.2f%%",
                            symbol,
                            distance
                        )

                        # IMPORTANT:
                        # Delete state after alert
                        del support_break_started[
                            symbol
                        ]

            # Too late
            elif (
                distance
                > MAX_BREAKOUT_DISTANCE
            ):

                del support_break_started[
                    symbol
                ]

    # =====================================================
    # RESISTANCE BREAKOUT
    # =====================================================

    if resistance is not None:

        crossed_resistance = (
            previous_price <= resistance
            and price > resistance
        )

        if crossed_resistance:

            resistance_break_started[symbol] = {
                "window": window,
                "level": resistance
            }

        started = (
            resistance_break_started.get(
                symbol
            )
        )

        if started:

            level = started["level"]

            distance = (
                (price - level)
                / level
                * 100
            )

            if (
                BREAKOUT_THRESHOLD
                <= distance
                <= MAX_BREAKOUT_DISTANCE
            ):

                if (
                    alerted_resistance.get(symbol)
                    != window
                ):

                    message = alert_message(
                        symbol,
                        "resistance",
                        level,
                        price,
                        distance,
                        volume
                    )

                    if send_telegram(message):

                        alerted_resistance[symbol] = (
                            window
                        )

                        logger.info(
                            "RESISTANCE BREAKOUT %s %.2f%%",
                            symbol,
                            distance
                        )

                        del resistance_break_started[
                            symbol
                        ]

            elif (
                distance
                > MAX_BREAKOUT_DISTANCE
            ):

                del resistance_break_started[
                    symbol
                ]

    # -----------------------------------------------------
    # Save current price AFTER checks
    # -----------------------------------------------------

    previous_prices[symbol] = price


# =========================================================
# CLEAN MEMORY
# =========================================================

def cleanup():

    window = current_window()

    old_window = window - 4

    for symbol in list(
        alerted_support.keys()
    ):

        if (
            alerted_support[symbol]
            < old_window
        ):

            del alerted_support[symbol]

    for symbol in list(
        alerted_resistance.keys()
    ):

        if (
            alerted_resistance[symbol]
            < old_window
        ):

            del alerted_resistance[symbol]


# =========================================================
# SCAN
# =========================================================

def scan():

    symbols = get_symbols()

    if not symbols:
        return

    volumes = get_volumes()

    prices = get_prices()

    if not prices:
        return

    eligible = []

    for symbol in symbols:

        volume = volumes.get(
            symbol,
            0
        )

        if volume < VOLUME_USDT_MIN:
            continue

        price = prices.get(symbol)

        if not price:
            continue

        eligible.append(
            (
                symbol,
                price,
                volume
            )
        )

    logger.info(
        "Eligible: %s",
        len(eligible)
    )

    for symbol, price, volume in eligible:

        try:

            check_symbol(
                symbol,
                price,
                volume
            )

        except Exception as e:

            logger.exception(
                "%s error: %s",
                symbol,
                e
            )

    cleanup()


# =========================================================
# MAIN
# =========================================================

def main():

    logger.info(
        "======================================"
    )

    logger.info(
        "MAJOR LEVEL BOT V3 STARTED"
    )

    logger.info(
        "Volume >= $%s",
        f"{VOLUME_USDT_MIN:,.0f}"
    )

    logger.info(
        "Breakout >= %.2f%%",
        BREAKOUT_THRESHOLD
    )

    logger.info(
        "Max distance = %.2f%%",
        MAX_BREAKOUT_DISTANCE
    )

    logger.info(
        "Scan every %ss",
        SCAN_INTERVAL_SEC
    )

    logger.info(
        "======================================"
    )

    while True:

        started = time.time()

        try:

            scan()

        except Exception as e:

            logger.exception(
                "SCAN ERROR: %s",
                e
            )

        elapsed = (
            time.time() - started
        )

        sleep_for = max(
            1,
            SCAN_INTERVAL_SEC - elapsed
        )

        time.sleep(
            sleep_for
        )


# =========================================================
# START
# =========================================================

if __name__ == "__main__":
    main()
