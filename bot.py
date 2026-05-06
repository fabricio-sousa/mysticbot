# =============================================================================
#  🪄  MAGICK BOT v5.7.0
#  Kalshi BTC 15-Minute Binary Pursuit Bot
#
#  Strategy: Buy YES or NO contracts at 93–95c (2–6 min before expiry)
#  with slippage=2 for instant taker fills. Guards prevent entry during
#  RSI extremes, high volatility, or dangerous time windows.
#
#  Keyboard controls (Windows only):
#    ESC = exit cleanly
#    C   = clear current trade state (manual override)
# =============================================================================

import os
import json
import time
import uuid
import requests
from datetime import datetime
import pytz
from kalshi_python_sync import Configuration, KalshiClient

# Windows-only: sounds and keyboard detection
try:
    import winsound
    import msvcrt
    HAS_WINDOWS = True
except ImportError:
    HAS_WINDOWS = False


# =============================================================================
#  CONFIGURATION — Update these values as your balance grows
# =============================================================================

# --- File paths (auto-detected relative to this script) ---
BASE_DIR     = os.path.dirname(os.path.abspath(__file__))
APIKEY_FILE  = os.path.join(BASE_DIR, "apikey.txt")   # Kalshi API key ID
PRIVATE_FILE = os.path.join(BASE_DIR, "private.txt")  # Kalshi private key (PEM)
LOG_FILE     = os.path.join(BASE_DIR, "log.txt")      # Human-readable activity log
STATE_FILE   = os.path.join(BASE_DIR, "state.json")   # Bot state (survives restarts)
TRADES_FILE  = os.path.join(BASE_DIR, "trades.json")  # Trade history for dashboard

# --- Order settings ---
MAX_SLIPPAGE         = 2      # cents above target price sent to Kalshi
                               # slippage=2 = taker order → instant full fills
                               # DO NOT set to 0 — causes partial fills and ghost fills
MAX_POSITION_DOLLARS = 300.0  # max dollars to deploy per trade (hard cap)
MAX_CONTRACTS        = 200    # max contracts per trade (hard cap)
                               # Both caps apply simultaneously — whichever is smaller wins

# --- Safety limits ---
SAFETY_FLOOR  = 1500          # bot shuts down permanently if balance drops below this
STRIKE_LIMIT  = 3             # max consecutive uncleared losses before shutdown

# --- Strike clearing system ---
# A loss adds 1 strike. You need STRIKE_CLEAR_WINS consecutive wins to remove it.
# Any loss during the clearing streak resets the win counter back to 0.
# Example: LOSS → need 3 wins → WIN → WIN → LOSS → restart (need 3 again)
STRIKE_CLEAR_WINS = 3

# --- Stop-loss ---
# If a position's live bid drops below (entry_price × threshold), exit early.
# Example at 0.45: bought at 94c → stop triggers if bid falls below 51.7c
STOP_LOSS_THRESHOLD = 0.45

# --- Session drawdown guard ---
# If total session PnL drops this low, pause trading for DRAWDOWN_PAUSE_MINUTES.
# Protects against alternating win/loss patterns that the strike system can't catch.
SESSION_DRAWDOWN_LIMIT = -400.0  # pause if session loses more than $400
DRAWDOWN_PAUSE_MINUTES = 30      # how long to pause (minutes)

# Internal tracking variables (do not edit)
OVERRIDE_TRIGGERED   = False
SESSION_PNL          = 0.00
_drawdown_pause_until = None


# =============================================================================
#  RSI LIMITS BY TIME WINDOW
#  RSI measures BTC momentum. Extreme RSI = BTC moving hard in one direction
#  = binary bet is more like a coin flip = skip entry.
#  Different windows use different bands based on typical market behavior.
# =============================================================================

RSI_PERIOD = 9  # candles used to calculate RSI (shorter = more responsive)

RSI_LIMITS_BY_WINDOW = {
    #                (low,  high)  — entry only allowed when RSI is between these values
    "overnight":  (25, 75),  # 12AM–5AM  — Asian session, calm, wide band OK
    "asian_open": (25, 75),  # 10PM–12AM — similar quiet character
    "weekend":    (30, 70),  # Sat/Sun   — moderate activity, slightly tighter
    "default":    (38, 62),  # US hours  — most active, tightest band
    "evening":    (42, 58),  # 5:30–10PM — re-enabled but strictest band
                              #             monitor win rate before widening
}

def get_rsi_limits() -> tuple:
    """
    Returns the (low, high) RSI limits for the current time window.
    Called every tick to apply the right filter for the time of day.
    """
    tz  = pytz.timezone("US/Eastern")
    now = datetime.now(tz)
    day = now.weekday()                     # 0=Monday, 5=Saturday, 6=Sunday
    tf  = now.hour + (now.minute / 60.0)   # time as a float, e.g. 14.5 = 2:30 PM

    if day in (5, 6):        return RSI_LIMITS_BY_WINDOW["weekend"]
    if 0.0  <= tf <  5.0:   return RSI_LIMITS_BY_WINDOW["overnight"]
    if 17.5 <= tf <  22.0:  return RSI_LIMITS_BY_WINDOW["evening"]
    if 22.0 <= tf <  24.0:  return RSI_LIMITS_BY_WINDOW["asian_open"]
    return RSI_LIMITS_BY_WINDOW["default"]

# After RSI was extreme (e.g. 80+), wait this many stable ticks before re-entering.
# Prevents jumping back in immediately after a spike — the "dead cat bounce" trap.
RSI_RECOVERY_TICKS = 4   # ~4 seconds of in-range RSI required before entry allowed

# --- Volatility guard ---
# Measures the BTC price range (high - low) over the last 5 one-minute candles.
# A large range = BTC is moving fast = binary outcome is unpredictable = skip.
VOLATILITY_CANDLES = 5
VOLATILITY_LIMIT   = 300  # skip entry if 5-candle range exceeds $300


# =============================================================================
#  DYNAMIC RISK ENGINE
#  Automatically adjusts position size based on current balance.
#  Smaller balance = higher risk % (need to grow faster).
#  Larger balance = lower risk % (protect what you've built).
# =============================================================================

def get_balance_tier(cash: float) -> dict:
    """
    Returns the risk tier for the current balance.
    Each tier has different risk percentages for each time window.
    The 'label' field is shown in the heartbeat display.
    """
    if cash < 300:
        # Tiny balance — use aggressive sizing to grow quickly
        return {
            "overnight": 0.25, "high": 0.25, "mid": 0.25, "weekend": 0.25,
            "label": "Recovery (<$300)"
        }
    elif cash < 600:
        # Small balance — still aggressive but with slightly more protection
        return {
            "overnight": 0.15, "high": 0.15, "mid": 0.12, "weekend": 0.12,
            "label": "Building (<$600)"
        }
    elif cash < 1500:
        # Growing balance — balanced approach, original proven settings
        return {
            "overnight": 0.10, "high": 0.15, "mid": 0.10, "weekend": 0.08,
            "label": "Growth (<$1500)"
        }
    elif cash < 5000:
        # Established balance — conservative, protect what you've earned
        return {
            "overnight": 0.08, "high": 0.12, "mid": 0.08, "weekend": 0.06,
            "label": "Established (<$5000)"
        }
    else:
        # Mature balance — capital preservation is the priority
        return {
            "overnight": 0.05, "high": 0.10, "mid": 0.07, "weekend": 0.05,
            "label": "Mature ($5000+)"
        }

def get_dynamic_risk(cash: float = 0):
    """
    Returns (risk_decimal, is_trading_window) for the current moment.

    risk_decimal    — what fraction of balance to risk (e.g. 0.08 = 8%)
    is_trading_window — True if the bot should be actively looking for entries

    Windows map to tier keys: "overnight", "high", "mid", "weekend"
    Skipped windows return (0.01, False) — bot idles until next window.
    """
    tz         = pytz.timezone("US/Eastern")
    now        = datetime.now(tz)
    day        = now.weekday()
    time_float = now.hour + (now.minute / 60.0)
    tier       = get_balance_tier(cash)

    # ── Weekdays (Monday–Friday) ──────────────────────────────────────────────
    if 0 <= day <= 4:
        if  0.0 <= time_float <  5.0:  return tier["overnight"], True   # 12AM–5AM  Overnight
        if  5.0 <= time_float <  8.5:  return 0.01,              False  # 5AM–8:30  Pre-market (skip)
        if 10.5 <= time_float < 12.0:  return tier["high"],      True   # 10:30AM   High Confidence open
        if 12.0 <= time_float < 16.0:  return tier["mid"],       True   # 12PM–4PM  Balanced Midday
        if 16.5 <= time_float < 17.5:  return tier["high"],      True   # 4:30–5:30 Primary Window
        if 17.5 <= time_float < 22.0:  return tier["mid"],       True   # 5:30–10PM Evening (tight RSI)
        if 22.0 <= time_float < 24.0:  return tier["overnight"], True   # 10PM–12AM Asian Open

    # ── Saturday ──────────────────────────────────────────────────────────────
    elif day == 5:
        if  0.0 <= time_float <  5.0:  return tier["overnight"], True   # Overnight
        if  5.0 <= time_float <  8.5:  return 0.01,              False  # Pre-market (skip)
        if  8.5 <= time_float < 17.0:  return tier["weekend"],   True   # Daytime
        if 22.0 <= time_float < 24.0:  return tier["overnight"], True   # Asian Open

    # ── Sunday ────────────────────────────────────────────────────────────────
    elif day == 6:
        if  0.0 <= time_float <  5.0:  return tier["overnight"], True   # Overnight
        if  5.0 <= time_float <  8.5:  return 0.01,              False  # Pre-market (skip)
        if  8.5 <= time_float < 17.0:  return tier["weekend"],   True   # Daytime
        if 22.0 <= time_float < 24.0:  return tier["overnight"], True   # Asian Open

    return 0.01, False  # All other times (8:30AM–10:30AM gap, Sunday evening) — idle


# =============================================================================
#  MARKET DATA — RSI & VOLATILITY
#  Both pulled from Bitfinex public API (no auth needed, 1-minute candles).
#  Returns a safe default if the API fails so the bot never crashes on a
#  network hiccup.
# =============================================================================

def get_btc_rsi() -> float:
    """
    Calculates the RSI-9 from the last 9 one-minute BTC/USD candles.
    RSI > 70 = overbought (BTC rising fast) → likely skip YES entry
    RSI < 30 = oversold (BTC falling fast) → likely skip NO entry
    Returns 50.0 (neutral) if the API call fails.
    """
    try:
        url    = f"https://api-pub.bitfinex.com/v2/candles/trade:1m:tBTCUSD/hist?limit={RSI_PERIOD + 10}"
        resp   = requests.get(url, timeout=5).json()
        closes = [c[2] for c in resp][::-1]  # close prices, oldest-first

        deltas   = [closes[i + 1] - closes[i] for i in range(len(closes) - 1)]
        gains    = [d if d > 0 else 0 for d in deltas]   # up moves
        losses   = [-d if d < 0 else 0 for d in deltas]  # down moves (positive value)
        avg_gain = sum(gains[-RSI_PERIOD:])  / RSI_PERIOD
        avg_loss = sum(losses[-RSI_PERIOD:]) / RSI_PERIOD

        if avg_loss == 0:
            return 100.0  # pure uptrend — extreme overbought

        rs = avg_gain / avg_loss
        return round(100 - (100 / (1 + rs)), 1)
    except Exception:
        return 50.0  # fail safe — neutral, won't block entry

def get_btc_volatility() -> float:
    """
    Returns the BTC high-low price range over the last 5 one-minute candles.
    High range = market is moving fast = binary outcome is harder to predict.
    Returns 0.0 (no block) if the API call fails.
    """
    try:
        url     = f"https://api-pub.bitfinex.com/v2/candles/trade:1m:tBTCUSD/hist?limit={VOLATILITY_CANDLES + 2}"
        resp    = requests.get(url, timeout=5).json()
        candles = resp[:VOLATILITY_CANDLES]
        highs   = [c[3] for c in candles]
        lows    = [c[4] for c in candles]
        return round(max(highs) - min(lows), 2)
    except Exception:
        return 0.0  # fail open — don't block entry on network error


# =============================================================================
#  HELPER FUNCTIONS
# =============================================================================

def log(msg: str):
    """Prints a timestamped message to console and appends it to log.txt."""
    ts = datetime.now(pytz.timezone("US/Eastern")).strftime("%Y-%m-%d %H:%M:%S ET")
    print(f"\n[{ts}] {msg}")
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(f"[{ts}] {msg}\n")

def load_state() -> dict:
    """
    Loads bot state from state.json (strikes, current open trade, etc).
    If the file doesn't exist or is corrupt, returns a clean default state.
    This lets the bot survive restarts without losing track of open positions.
    """
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            try:
                return json.load(f)
            except Exception:
                pass  # file corrupt — fall through to default
    return {"strikes": 0, "consecutive_wins": 0, "current_trade": None}

def save_state(state: dict):
    """Saves current bot state to state.json (overwrites each time)."""
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)

def update_trades_json(trade_entry: dict):
    """
    Appends a completed trade record to trades.json.
    Used by the dashboard to display PnL history and stats.
    Each entry has: timestamp, ticker, side, pnl, type (SETTLEMENT or STOP_LOSS)
    """
    trades = []
    if os.path.exists(TRADES_FILE):
        with open(TRADES_FILE, "r") as f:
            try:
                trades = json.load(f)
            except Exception:
                trades = []
    trades.append(trade_entry)
    with open(TRADES_FILE, "w") as f:
        json.dump(trades, f, indent=2)

def safe_price_cents(value) -> int:
    """
    Converts a Kalshi dollar price (e.g. 0.9400) to cents (e.g. 94).
    Returns 0 if the value is None, empty, or invalid.
    """
    try:
        return int(round(float(value or 0) * 100))
    except Exception:
        return 0

def play_sound(event_type: str):
    """
    Plays a Windows beep sound for key bot events.
    Does nothing on non-Windows systems.
      buy         = entry confirmed (high beep)
      settle_win  = contract settled as profit (two ascending beeps)
      settle_loss = contract settled as loss (low long beep)
      stop        = stop-loss triggered (very low long beep)
    """
    if not HAS_WINDOWS:
        return
    sounds = {
        "buy":         [(2000, 200)],
        "settle_win":  [(2500, 200), (3000, 200)],
        "settle_loss": [(600, 500)],
        "stop":        [(400, 1000)],
    }
    for freq, duration in sounds.get(event_type, []):
        winsound.Beep(freq, duration)

def parse_order(order) -> tuple:
    """
    Extracts fill details from a Kalshi order object.
    Returns (filled_qty, avg_price_cents, fill_cost_dollars).
    Returns (0, 0, 0.0) if the order hasn't filled yet.

    Why this is careful:
    - Kalshi returns fill_count_fp as a string like '250.00' or '37.97'
    - Fractional contracts (e.g. 37.97) can distort price if we truncate early
    - We use qty_raw (float) for price math, qty (int) for contract counts
    - Binary contracts must price 1–99c — anything outside is a parse error
    """
    try:
        qty_raw = float(getattr(order, 'fill_count_fp', '0') or '0')
        qty     = int(qty_raw)   # whole number for order sizing
        if qty <= 0:
            return 0, 0, 0.0

        # Kalshi fills can be as taker OR maker — check both cost fields
        taker = float(getattr(order, 'taker_fill_cost_dollars', '0') or '0')
        maker = float(getattr(order, 'maker_fill_cost_dollars', '0') or '0')
        cost  = taker if taker > 0 else maker

        if cost == 0:
            log("⚠️ Both taker and maker fill cost are 0 — PnL will be inaccurate.")

        # Use qty_raw (not truncated qty) to avoid inflating the price on fractional fills
        avg_cents = int(round((cost / qty_raw) * 100)) if cost > 0 else 0

        # Sanity check: binary contract prices must be between 1¢ and 99¢
        if avg_cents < 1 or avg_cents > 99:
            log(f"🚨 parse_order: suspicious price {avg_cents}c (cost=${cost:.4f}, qty={qty_raw}) — rejecting fill.")
            return 0, 0, 0.0

        return qty, avg_cents, round(cost, 4)
    except Exception:
        return 0, 0, 0.0


# =============================================================================
#  KALSHI API CONNECTION
#  Reads credentials from apikey.txt and private.txt in the bot folder.
# =============================================================================

with open(APIKEY_FILE,  "r", encoding="utf-8") as f: api_key_id      = f.read().strip()
with open(PRIVATE_FILE, "r", encoding="utf-8") as f: private_key_pem = f.read()

config                 = Configuration(host="https://api.elections.kalshi.com/trade-api/v2")
config.api_key_id      = api_key_id
config.private_key_pem = private_key_pem
client                 = KalshiClient(config)


def place_order(ticker, side, count, action, price_cents=None):
    """
    Places a limit order on Kalshi and returns fill details.
    Returns (success, avg_cents, filled_qty, fill_cost_dollars).

    Uses MAX_SLIPPAGE=2 which makes this a TAKER order:
    - On a buy:  sends limit at (price + 2c) → immediately hits the ask
    - On a sell: sends limit at (price - 2c) → immediately hits the bid
    - Result: instant full fills, no partial fill complexity

    After the create response, polls up to 3 times (4.5s total) in the rare
    case the fill confirmation is slightly delayed.
    """
    try:
        order_id     = str(uuid.uuid4())  # unique ID to track this specific order
        actual_limit = (
            min(99, price_cents + MAX_SLIPPAGE) if action == "buy"
            else max(1,  price_cents - MAX_SLIPPAGE)
        )

        resp = client.create_order(
            ticker         = ticker,
            side           = side,
            action         = action,
            count          = count,
            type           = "limit",
            client_order_id = order_id,
            yes_price      = actual_limit if side == "yes" else None,
            no_price       = actual_limit if side == "no"  else None,
        )

        order     = resp.order
        target_id = order.order_id

        # Check if Kalshi already confirmed the fill in the create response
        # (this happens ~95% of the time with taker orders)
        qty, avg_cents, fill_cost = parse_order(order)
        if qty > 0:
            log(f"⚡ Instant fill detected in create response: {qty} @ {avg_cents}c")
            return True, avg_cents, qty, fill_cost

        # Rare: fill not yet confirmed — poll a few times before giving up
        for attempt in range(3):
            time.sleep(1.5)
            order_info = client.get_order(target_id).order
            status     = getattr(order_info, 'status', None)
            qty, avg_cents, fill_cost = parse_order(order_info)

            if qty > 0:
                log(f"⏱️ Fill confirmed after {(attempt + 1) * 1.5:.1f}s polling: {qty} @ {avg_cents}c")
                return True, avg_cents, qty, fill_cost

            if status is not None and str(status).lower() in ['canceled', 'expired']:
                log(f"ℹ️ Order {target_id} {status} during polling.")
                break

        return False, 0, 0, 0.0

    except Exception as e:
        log(f"❌ Order Error: {e}")
        return False, 0, 0, 0.0


# =============================================================================
#  MAIN LOOP — Session state variables
#  These live in memory only — reset each time the bot restarts.
# =============================================================================

_last_skip_reason  = None   # suppresses repeated skip messages (only logs first)
_rsi_stable_ticks  = 0      # how many consecutive ticks RSI has been in safe zone
_entry_lock        = False   # prevents two entry attempts firing in the same tick
_locked_tickers    = set()   # markets already traded — never re-entered this session
_wins_since_strike = 0       # consecutive wins toward clearing the current strike


# =============================================================================
#  START
# =============================================================================

if __name__ == "__main__":
    log("🪄 Magick Bot v5.7.0 Active | Slippage=2 (taker) | All guards active")

    while True:
        try:

            # ── Keyboard input (Windows only) ────────────────────────────────
            if HAS_WINDOWS and msvcrt.kbhit():
                key = msvcrt.getch()
                if key == b'\x1b':
                    os._exit(0)                  # ESC = hard exit
                elif key.lower() == b'c':
                    OVERRIDE_TRIGGERED = True     # C = clear state (manual override)

            # ── Snapshot current conditions ───────────────────────────────────
            now_et             = datetime.now(pytz.timezone("US/Eastern"))
            state              = load_state()
            cash               = client.get_balance().balance / 100.0  # Kalshi returns cents
            curr               = state.get("current_trade")             # currently open position (or None)
            risk_decimal, is_trading_window = get_dynamic_risk(cash)
            current_rsi        = get_btc_rsi()
            current_volatility = get_btc_volatility()

            # ── Manual override (C key pressed) ───────────────────────────────
            if OVERRIDE_TRIGGERED:
                log("🛠️ Manual Override: Clearing State")
                state["current_trade"] = None
                save_state(state)
                OVERRIDE_TRIGGERED = False

            # ── Hard shutdown checks ──────────────────────────────────────────
            # Shuts down the bot permanently if balance is too low or too many
            # consecutive uncleared losses have occurred.
            if cash <= SAFETY_FLOOR or state.get("strikes", 0) >= STRIKE_LIMIT:
                log(f"🚨 Shutdown: Cash ${cash:.2f} | Strikes {state.get('strikes')}")
                break

            # ── Session drawdown pause ────────────────────────────────────────
            # If this session has lost more than SESSION_DRAWDOWN_LIMIT, pause
            # trading for DRAWDOWN_PAUSE_MINUTES before resuming.
            if SESSION_PNL <= SESSION_DRAWDOWN_LIMIT:
                if _drawdown_pause_until is None or now_et >= _drawdown_pause_until:
                    import datetime as _dt
                    _drawdown_pause_until = now_et + _dt.timedelta(minutes=DRAWDOWN_PAUSE_MINUTES)
                    log(f"⏸️ DRAWDOWN PAUSE: Session ${SESSION_PNL:+.2f} hit limit. Pausing {DRAWDOWN_PAUSE_MINUTES}m.")
                    play_sound("stop")
                if now_et < _drawdown_pause_until:
                    remaining = (_drawdown_pause_until - now_et).seconds // 60
                    print(f"\r⏸️ DRAWDOWN PAUSE — {remaining}m remaining | Session: ${SESSION_PNL:+.2f}{'':>50}",
                          end="", flush=True)
                    time.sleep(30)
                    continue
                else:
                    log(f"▶️ Drawdown pause expired — resuming. Session: ${SESSION_PNL:+.2f}")
                    _drawdown_pause_until = None

            # ── Fetch open KXBTC15M markets ───────────────────────────────────
            # Gets the next 1–5 open markets, filters to ones not yet expired.
            resp    = client.get_markets(series_ticker="KXBTC15M", limit=5, status="open")
            markets = [m for m in getattr(resp, 'markets', [])
                       if (m.close_time - now_et).total_seconds() > 0]

            if markets:
                markets.sort(key=lambda x: x.close_time)
                market    = markets[0]   # always work with the nearest-expiry market
                time_left = (market.close_time - now_et).total_seconds() / 60.0
                y_p       = safe_price_cents(market.yes_bid_dollars)  # YES bid in cents
                n_p       = safe_price_cents(market.no_bid_dollars)   # NO  bid in cents
            else:
                time_left = 0

            # ── Stop-loss monitor (wick protection) ───────────────────────────
            # If we have an open position, check if the live bid has fallen far
            # enough to trigger the stop-loss. We confirm the drop over 2 seconds
            # before selling to avoid selling on a brief 1-tick wick that recovers.
            if curr and curr.get("status") == "filled":
                m_live   = client.get_market(curr['ticker']).market
                live_bid = safe_price_cents(
                    m_live.yes_bid_dollars if curr['side'] == "yes"
                    else m_live.no_bid_dollars
                )
                entry_p = curr['actual_entry_price']
                stop_p  = round(entry_p * (1 - STOP_LOSS_THRESHOLD), 2)

                if 0 < live_bid <= stop_p and time_left > 0.5:
                    log(f"⚠️ STOP WARNING: {curr['ticker']} @ {live_bid}c (SL: {stop_p}c) — confirming in 2s...")
                    time.sleep(2)

                    # Re-check after 2 seconds — if it recovered, it was a wick
                    m_confirm   = client.get_market(curr['ticker']).market
                    confirm_bid = safe_price_cents(
                        m_confirm.yes_bid_dollars if curr['side'] == "yes"
                        else m_confirm.no_bid_dollars
                    )

                    if confirm_bid > stop_p:
                        log(f"✅ STOP CANCELLED: Recovered to {confirm_bid}c — wick detected, holding.")

                    else:
                        # Price confirmed below stop — execute the stop-loss sell
                        log(f"🚨 STOP LOSS CONFIRMED: Selling {curr['ticker']} ({confirm_bid}c | SL: {stop_p}c)")
                        success, actual_sell, filled_qty, _ = place_order(
                            curr['ticker'], curr['side'], curr['count'], "sell", confirm_bid
                        )

                        if not success or actual_sell == 0:
                            # Market closed before we could sell — let it settle naturally
                            log("⚠️ Stop-loss sell rejected (market closed) — awaiting settlement.")
                            state["strikes"]          = state.get("strikes", 0) + 1
                            state["consecutive_wins"] = 0
                            state["current_trade"]    = None
                            save_state(state)
                            play_sound("stop")
                            time.sleep(60)
                            continue

                        # Log a warning if the sell price was very different from expected
                        if abs(actual_sell - confirm_bid) > 20:
                            log(f"⚠️ Sell fill ({actual_sell}c) far from bid ({confirm_bid}c) — PnL may be off.")

                        # Calculate PnL: sell proceeds minus what we originally paid
                        sell_proceeds = actual_sell * filled_qty / 100.0
                        buy_cost = (
                            curr['actual_fill_cost_dollars']  # use stored cost if available
                            if curr.get('actual_fill_cost_dollars')
                            else entry_p * curr['count'] / 100.0
                        )
                        pnl = sell_proceeds - buy_cost

                        update_trades_json({
                            "timestamp": now_et.strftime("%Y-%m-%d %H:%M:%S"),
                            "ticker":    curr['ticker'],
                            "side":      curr['side'],
                            "pnl":       round(pnl, 2),
                            "type":      "STOP_LOSS",
                        })
                        SESSION_PNL        += pnl
                        state["strikes"]    = state.get("strikes", 0) + 1
                        _wins_since_strike  = 0   # loss resets the clearing streak
                        state["current_trade"] = None   # clear state AFTER successful sell
                        save_state(state)
                        play_sound("stop")
                        log(f"💸 Stop-loss complete. PnL: ${pnl:+.2f} | "
                            f"Strikes: {state['strikes']}/{STRIKE_LIMIT} | "
                            f"Need {STRIKE_CLEAR_WINS} wins to clear")
                        log("⏸️ Post-SL cooldown (60s)")
                        time.sleep(60)
                        continue

            # ── Heartbeat display ─────────────────────────────────────────────
            # Printed to console every tick — shows current state at a glance.
            # Uses \r (carriage return) to overwrite the same line each tick.
            tier_label = get_balance_tier(cash)["label"]
            vol_flag   = " ⚠️VOL" if current_volatility >= VOLATILITY_LIMIT else ""
            mkt_str    = (
                f" | Y:{y_p}c N:{n_p}c {time_left:.1f}m"
                if markets else " | no market"
            )
            pos_str = (
                f" [IN: {curr['side'].upper()} @ {curr.get('actual_entry_price')}c]"
                if curr else ""
            )
            heartbeat = (
                f"[{now_et.strftime('%H:%M:%S')}] {tier_label} | "
                f"Risk: {int(risk_decimal * 100)}% | "
                f"RSI: {current_rsi} | "
                f"Vol: ${current_volatility:.0f}{vol_flag}"
                f"{mkt_str} | "
                f"Cash: ${cash:.2f} | "
                f"Session: ${SESSION_PNL:+.2f}{pos_str}"
            )
            print(f"\r{heartbeat:<160}", end="", flush=True)

            # Skip the rest of the loop if not in a trading window or no market
            if not is_trading_window and not curr:
                time.sleep(10)
                continue
            if not markets:
                time.sleep(5)
                continue

            # ── Settlement check ──────────────────────────────────────────────
            # Detects when the market we're in has expired and a new one is active.
            # Waits up to 35s for Kalshi to publish the result, then records PnL.
            # If result never appears after 10 minutes, clears state and moves on
            # (protects against CF Benchmarks settlement outages).
            if curr and market.ticker != curr["ticker"]:

                # Track how long we've been waiting for this settlement
                if not curr.get("finalizing_since"):
                    curr["finalizing_since"] = now_et.timestamp()
                    save_state(state)

                waited_minutes = (now_et.timestamp() - curr["finalizing_since"]) / 60.0

                if waited_minutes > 10:
                    log(f"⚠️ Settlement timeout after {waited_minutes:.0f}m — "
                        f"{curr['ticker']} never resolved. Clearing state.")
                    state["current_trade"] = None
                    save_state(state)
                else:
                    log(f"⏳ Finalizing {curr['ticker']}... ({waited_minutes:.0f}m elapsed)")
                    time.sleep(35)  # give Kalshi time to post the result

                    res = getattr(
                        client.get_market(curr['ticker']).market, 'result', ''
                    ).lower()

                    if res in ['yes', 'no']:
                        won     = (curr['side'] == res)
                        entry_p = curr['actual_entry_price']
                        pnl     = (
                            (100 - entry_p) * curr['count'] / 100.0  # win: collect $1 per contract
                            if won
                            else -(entry_p * curr['count'] / 100.0)  # loss: forfeit cost
                        )

                        update_trades_json({
                            "timestamp": now_et.strftime("%Y-%m-%d %H:%M:%S"),
                            "ticker":    curr['ticker'],
                            "side":      curr['side'],
                            "pnl":       round(pnl, 2),
                            "type":      "SETTLEMENT",
                        })
                        SESSION_PNL += pnl

                        # ── 3-win strike clear system ─────────────────────────
                        # A strike is only cleared after STRIKE_CLEAR_WINS
                        # consecutive wins. Any loss resets the counter.
                        if won:
                            if state.get("strikes", 0) > 0:
                                _wins_since_strike += 1
                                if _wins_since_strike >= STRIKE_CLEAR_WINS:
                                    state["strikes"]   = max(0, state["strikes"] - 1)
                                    _wins_since_strike = 0
                                    log(f"🏁 {res.upper()} | WIN ✅ | PnL: ${pnl:+.2f} | "
                                        f"✅ Strike cleared! Remaining: {state['strikes']}/{STRIKE_LIMIT}")
                                else:
                                    log(f"🏁 {res.upper()} | WIN ✅ | PnL: ${pnl:+.2f} | "
                                        f"Strikes: {state['strikes']}/{STRIKE_LIMIT} | "
                                        f"Clearing: {_wins_since_strike}/{STRIKE_CLEAR_WINS} wins")
                            else:
                                _wins_since_strike = 0
                                log(f"🏁 {res.upper()} | WIN ✅ | PnL: ${pnl:+.2f} | "
                                    f"Strikes: 0 | Session: ${SESSION_PNL:+.2f}")
                        else:
                            state["strikes"]   = state.get("strikes", 0) + 1
                            _wins_since_strike = 0
                            log(f"🏁 {res.upper()} | LOSS ❌ | PnL: ${pnl:+.2f} | "
                                f"Strikes: {state['strikes']}/{STRIKE_LIMIT} | "
                                f"Need {STRIKE_CLEAR_WINS} wins to clear")

                        _locked_tickers.add(curr['ticker'])  # never re-enter this market
                        state["current_trade"] = None
                        save_state(state)
                        play_sound("settle_win" if won else "settle_loss")

            # ── Entry logic ───────────────────────────────────────────────────
            # Checks all conditions and places a buy order if everything aligns.
            elif not curr and is_trading_window:

                # Refresh prices right before entry to avoid acting on stale data
                try:
                    fresh     = client.get_market(market.ticker).market
                    y_p       = safe_price_cents(fresh.yes_bid_dollars)
                    n_p       = safe_price_cents(fresh.no_bid_dollars)
                    time_left = (fresh.close_time - now_et).total_seconds() / 60.0
                except Exception:
                    pass  # use values from earlier in the tick if refresh fails

                # Ticker lock: skip if we already traded this market this session
                # Prevents double-buying the same market on bot restart or state glitch
                if market.ticker in _locked_tickers:
                    time.sleep(1)
                    continue

                # Entry window: only enter between 2 and 6 minutes before expiry
                # at 93–95c (our target range with good EV at 97%+ win rate)
                if 2.0 <= time_left <= 6.0 and (93 <= y_p <= 95 or 93 <= n_p <= 95):
                    side, price = (
                        ("yes", y_p) if 93 <= y_p <= 95
                        else ("no", n_p)
                    )

                    rsi_low, rsi_high = get_rsi_limits()

                    # ── Guard: volatility too high ────────────────────────────
                    if current_volatility >= VOLATILITY_LIMIT:
                        _rsi_stable_ticks = 0
                        if _last_skip_reason != "VOL":
                            log(f"⏭️ Skipping {side.upper()}: volatility ${current_volatility:.0f} exceeds ${VOLATILITY_LIMIT}.")
                            _last_skip_reason = "VOL"

                    # ── Guard: RSI too low (oversold) ─────────────────────────
                    elif current_rsi < rsi_low:
                        _rsi_stable_ticks = 0
                        if _last_skip_reason != "RSI_LOW":
                            log(f"⏭️ Skipping {side.upper()}: RSI={current_rsi} below {rsi_low} (window limit).")
                            _last_skip_reason = "RSI_LOW"

                    # ── Guard: RSI too high (overbought) ──────────────────────
                    elif current_rsi > rsi_high:
                        _rsi_stable_ticks = 0
                        if _last_skip_reason != "RSI_HIGH":
                            log(f"⏭️ Skipping {side.upper()}: RSI={current_rsi} above {rsi_high} (window limit).")
                            _last_skip_reason = "RSI_HIGH"

                    # ── Guard: RSI recovery cooldown ──────────────────────────
                    # Even if RSI is back in range after an extreme, wait for it
                    # to stay stable for RSI_RECOVERY_TICKS ticks before entering
                    elif _rsi_stable_ticks < RSI_RECOVERY_TICKS:
                        _rsi_stable_ticks += 1
                        if _last_skip_reason != "RSI_RECOVERY":
                            log(f"⏭️ Skipping {side.upper()}: RSI recovery cooldown "
                                f"({_rsi_stable_ticks}/{RSI_RECOVERY_TICKS} ticks stable).")
                            _last_skip_reason = "RSI_RECOVERY"

                    # ── All guards passed — attempt entry ─────────────────────
                    else:
                        _last_skip_reason = None

                        # Position sizing: smaller of (risk% of balance) or MAX_POSITION_DOLLARS
                        # then capped at MAX_CONTRACTS
                        qty = int(min(MAX_POSITION_DOLLARS, cash * risk_decimal) * 100 // price)
                        qty = min(qty, MAX_CONTRACTS)

                        if qty >= 1 and not _entry_lock:
                            _entry_lock = True
                            log(f"⚡ Pursuit: {side.upper()} @ {price}c (Qty: {qty}) | RSI: {current_rsi}")

                            success, actual_paid, filled_qty, fill_cost = place_order(
                                market.ticker, side, qty, "buy", price
                            )

                            if success and filled_qty > 0:
                                # Fill confirmed — save position to state
                                state["current_trade"] = {
                                    "ticker":                  market.ticker,
                                    "side":                    side,
                                    "count":                   filled_qty,
                                    "entry_price_cents":       actual_paid,
                                    "actual_entry_price":      actual_paid,
                                    "actual_fill_cost_dollars": fill_cost,
                                    "status":                  "filled",
                                }
                                save_state(state)
                                _locked_tickers.add(market.ticker)
                                log(f"🔒 Ticker locked: {market.ticker}")
                                _entry_lock = False
                                play_sound("buy")
                                log(f"✅ Filled: {filled_qty} contracts @ {actual_paid}c")
                                time.sleep(5)  # brief pause after fill before next tick

                            else:
                                # Order didn't fill — cooldown before next attempt
                                _entry_lock = False
                                log("⚠️ Entry failed or zero fill. 15s Cooldown...")
                                time.sleep(15)

            # ── End of tick ───────────────────────────────────────────────────
            time.sleep(1)

        except Exception as e:
            log(f"⚠️ Loop Error: {e}")
            time.sleep(5)
