import os
import json
import time
import uuid
import requests
from datetime import datetime
import pytz
from kalshi_python_sync import Configuration, KalshiClient

# Windows-only tools
try:
    import winsound
    import msvcrt
    HAS_WINDOWS = True
except ImportError:
    HAS_WINDOWS = False

# ====================== CONFIG ======================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
APIKEY_FILE = os.path.join(BASE_DIR, "apikey.txt")
PRIVATE_FILE = os.path.join(BASE_DIR, "private.txt")
LOG_FILE = os.path.join(BASE_DIR, "log.txt")
STATE_FILE = os.path.join(BASE_DIR, "state.json")
TRADES_FILE = os.path.join(BASE_DIR, "trades.json")

MAX_SLIPPAGE = 2
MAX_POSITION_DOLLARS = 500.0   # hard cap per trade in dollars regardless of balance
MAX_CONTRACTS = 150            # hard cap on contracts per trade regardless of position size
SAFETY_FLOOR = 600.0          # bot shuts down if balance drops below $1200
STRIKE_LIMIT = 3
STOP_LOSS_THRESHOLD = 0.40
OVERRIDE_TRIGGERED = False
SESSION_PNL = 0.00

# --- RSI ---
RSI_PERIOD = 9

# RSI limits vary by time window — looser overnight/weekends (calmer markets),
# tighter during high-activity US hours where momentum is more dangerous.
# Format: (low_limit, high_limit)
RSI_LIMITS_BY_WINDOW = {
    "overnight":  (25, 75),   # 12AM–5AM  — Asian session, low vol, wide band
    "asian_open": (25, 75),   # 10PM–12AM — similar character to overnight
    "evening":    (30, 70),   # 5:30PM–10PM — reduced activity, moderate band
    "weekend":    (30, 70),   # Sat/Sun   — moderate, less macro risk
    "default":    (38, 62),   # All US hours — tightest, most momentum risk
}

def get_rsi_limits() -> tuple:
    """Return (low, high) RSI limits based on current time window."""
    tz = pytz.timezone("US/Eastern")
    now = datetime.now(tz)
    day = now.weekday()
    tf  = now.hour + (now.minute / 60.0)
    if day in (5, 6):                          return RSI_LIMITS_BY_WINDOW["weekend"]
    if 0.0  <= tf <  5.0:                      return RSI_LIMITS_BY_WINDOW["overnight"]
    if 17.5 <= tf <  22.0:                     return RSI_LIMITS_BY_WINDOW["evening"]
    if 22.0 <= tf <  24.0:                     return RSI_LIMITS_BY_WINDOW["asian_open"]
    return RSI_LIMITS_BY_WINDOW["default"]

# RSI recovery cooldown — if RSI was in extreme territory recently,
# wait for it to stay in the safe zone for this many consecutive ticks
# before allowing an entry. Prevents the "dead cat bounce" trap.
RSI_RECOVERY_TICKS = 4   # ~4 seconds of stable RSI required after extreme

# --- Volatility guard ---
# Max allowed BTC price range over last 5 candles before skipping entry.
# A $300+ move in 5 minutes signals a breakout/breakdown — avoid chasing.
VOLATILITY_CANDLES = 5
VOLATILITY_LIMIT   = 300  # dollars

# ====================== DYNAMIC RISK ENGINE ======================
# Balance-based risk tiers — automatically scales down as balance grows.
# Each tier defines (overnight, us_high, us_mid, weekend) risk multipliers.
# Pre-market is always skipped regardless of tier.
def get_balance_tier(cash: float) -> dict:
    if cash < 300:
        # Recovery mode — aggressive growth, small absolute risk
        return {"overnight": 0.25, "high": 0.25, "mid": 0.25, "weekend": 0.25, "label": "Recovery (<$300)"}
    elif cash < 600:
        # Building mode — still aggressive but with more to protect
        return {"overnight": 0.15, "high": 0.15, "mid": 0.12, "weekend": 0.12, "label": "Building (<$600)"}
    elif cash < 1500:
        # Growth mode — balanced, original proven settings
        return {"overnight": 0.10, "high": 0.15, "mid": 0.10, "weekend": 0.08, "label": "Growth (<$1500)"}
    elif cash < 5000:
        # Established mode — more conservative, protect larger balance
        return {"overnight": 0.08, "high": 0.12, "mid": 0.08, "weekend": 0.06, "label": "Established (<$5000)"}
    else:
        # Mature mode — capital preservation priority
        return {"overnight": 0.05, "high": 0.10, "mid": 0.07, "weekend": 0.05, "label": "Mature ($5000+)"}

def get_dynamic_risk(cash: float = 0):
    tz = pytz.timezone("US/Eastern")
    now = datetime.now(tz)
    day = now.weekday()   # 0=Mon ... 5=Sat, 6=Sun
    time_float = now.hour + (now.minute / 60.0)
    tier = get_balance_tier(cash)

    if 0 <= day <= 4:                                                              # Monday - Friday
        if  0.0 <= time_float <  5.0: return tier["overnight"], True              # Overnight
        if  5.0 <= time_float <  8.5: return 0.01, False                          # Pre-market — always skip
        if 10.5 <= time_float < 12.0: return tier["high"],      True              # High confidence open
        if 12.0 <= time_float < 16.0: return tier["mid"],       True              # Balanced midday
        if 16.5 <= time_float < 17.5: return tier["high"],      True              # Primary close window
        if 17.5 <= time_float < 20.0: return 0.05,            True              # Evening — fixed 5%
        if 22.0 <= time_float < 24.0: return tier["overnight"], True              # Asian open (7-day)

    elif day == 5:                                                                 # Saturday
        if  0.0 <= time_float < 10.0: return tier["overnight"], True              # Sat overnight
        if 10.0 <= time_float < 17.0: return tier["weekend"],   True              # Sat daytime
        if 22.0 <= time_float < 24.0: return tier["overnight"], True              # Sat Asian open

    elif day == 6:                                                                 # Sunday
        if  0.0 <= time_float < 17.0: return tier["weekend"],   True              # Sun all day
        if 22.0 <= time_float < 24.0: return tier["overnight"], True              # Sun Asian open

    return 0.01, False   # All other times — skip

# ====================== RSI ======================
def get_btc_rsi() -> float:
    try:
        url  = f"https://api-pub.bitfinex.com/v2/candles/trade:1m:tBTCUSD/hist?limit={RSI_PERIOD + 10}"
        resp = requests.get(url, timeout=5).json()
        closes   = [c[2] for c in resp][::-1]
        deltas   = [closes[i + 1] - closes[i] for i in range(len(closes) - 1)]
        gains    = [d if d > 0 else 0 for d in deltas]
        losses   = [-d if d < 0 else 0 for d in deltas]
        avg_gain = sum(gains[-RSI_PERIOD:]) / RSI_PERIOD
        avg_loss = sum(losses[-RSI_PERIOD:]) / RSI_PERIOD
        if avg_loss == 0:
            return 100.0
        rs = avg_gain / avg_loss
        return round(100 - (100 / (1 + rs)), 1)
    except Exception:
        return 50.0

def get_btc_volatility() -> float:
    """
    Returns the BTC high-low range over the last VOLATILITY_CANDLES 1-minute
    candles. A large range means a breakout/breakdown is in progress — skip entry.
    Falls back to 0.0 (no block) if the API call fails.
    """
    try:
        url  = f"https://api-pub.bitfinex.com/v2/candles/trade:1m:tBTCUSD/hist?limit={VOLATILITY_CANDLES + 2}"
        resp = requests.get(url, timeout=5).json()
        candles = resp[:VOLATILITY_CANDLES]
        highs = [c[3] for c in candles]
        lows  = [c[4] for c in candles]
        return round(max(highs) - min(lows), 2)
    except Exception:
        return 0.0   # fail open — don't block on API error

# ====================== HELPERS ======================
def log(msg: str):
    ts = datetime.now(pytz.timezone("US/Eastern")).strftime("%Y-%m-%d %H:%M:%S ET")
    print(f"\n[{ts}] {msg}")
    with open(LOG_FILE, "a", encoding="utf-8") as f: f.write(f"[{ts}] {msg}\n")

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            try: return json.load(f)
            except: pass
    return {"strikes": 0, "current_trade": None}

def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f: json.dump(state, f, indent=2)

def update_trades_json(trade_entry):
    trades = []
    trade_entry["category"] = "bot"
    if os.path.exists(TRADES_FILE):
        with open(TRADES_FILE, "r") as f:
            try: trades = json.load(f)
            except: trades = []
    trades.append(trade_entry)
    with open(TRADES_FILE, "w") as f: json.dump(trades, f, indent=2)

def safe_price_cents(value) -> int:
    try: return int(round(float(value or 0) * 100))
    except: return 0

def play_sound(event_type):
    if not HAS_WINDOWS: return
    s = {"buy": [(2000, 200)], "settle_win": [(2500, 200), (3000, 200)], "settle_loss": [(600, 500)], "stop": [(400, 1000)]}
    for f, d in s.get(event_type, []): winsound.Beep(f, d)

def parse_order(order) -> tuple[int, int]:
    """
    Extract (filled_qty, avg_price_cents) from an order object.
    Fields confirmed via debug:
      - fill_count_fp:           contracts filled, string like '32.00'
      - taker_fill_cost_dollars: total cost when filled as taker
      - maker_fill_cost_dollars: total cost when filled as maker
    Orders can fill as either taker or maker — checks both.
    Returns (0, 0) if unfilled.
    """
    try:
        qty = int(float(getattr(order, 'fill_count_fp', '0') or '0'))
        if qty <= 0:
            return 0, 0
        taker = float(getattr(order, 'taker_fill_cost_dollars', '0') or '0')
        maker = float(getattr(order, 'maker_fill_cost_dollars', '0') or '0')
        cost  = taker if taker > 0 else maker
        if cost == 0:
            log("⚠️ Both taker and maker fill cost are 0 — entry price unknown, PnL will be inaccurate.")
        avg_cents = int(round((cost / qty) * 100)) if cost > 0 else 0
        return qty, avg_cents
    except Exception:
        return 0, 0

# ====================== API SETUP ======================
with open(APIKEY_FILE, "r", encoding="utf-8") as f: api_key_id = f.read().strip()
with open(PRIVATE_FILE, "r", encoding="utf-8") as f: private_key_pem = f.read()

config = Configuration(host="https://api.elections.kalshi.com/trade-api/v2")
config.api_key_id = api_key_id
config.private_key_pem = private_key_pem
client = KalshiClient(config)

def place_order(ticker, side, count, action, price_cents=None):
    try:
        order_id = str(uuid.uuid4())
        actual_limit = min(99, price_cents + MAX_SLIPPAGE) if action == "buy" else max(1, price_cents - MAX_SLIPPAGE)

        resp = client.create_order(
            ticker=ticker, side=side, action=action, count=count, type="limit",
            client_order_id=order_id,
            yes_price=actual_limit if side == "yes" else None,
            no_price=actual_limit if side == "no" else None
        )

        order     = resp.order
        target_id = order.order_id

        # Check if already filled in the create response
        qty, avg_cents = parse_order(order)
        if qty > 0:
            log(f"⚡ Instant fill detected in create response: {qty} @ {avg_cents}c")
            return True, avg_cents, qty

        # Not filled yet — poll for fill
        for _ in range(5):
            time.sleep(1.5)
            order_info = client.get_order(target_id).order
            status     = getattr(order_info, 'status', None)
            qty, avg_cents = parse_order(order_info)
            if qty > 0:
                return True, avg_cents, qty
            if status is not None and str(status).lower() in ['canceled', 'expired']:
                log(f"ℹ️ Order {target_id} {status} during polling.")
                break

        return False, 0, 0

    except Exception as e:
        log(f"❌ Order Error: {e}")
        return False, 0, 0

# ====================== MAIN LOOP ======================
_last_skip_reason      = None   # tracks last skip reason to suppress log spam
_rsi_stable_ticks      = 0      # counts consecutive ticks with RSI in safe zone
_entry_lock            = False  # in-memory lock prevents double-buy race condition
_manual_trade_trigger  = False  # T key pressed — place one manual trade at 25% this session

if __name__ == "__main__":
    log("🪄 Magick Bot v5.4.9 Active (Manual Trade Key)")

    while True:
        try:
            if HAS_WINDOWS and msvcrt.kbhit():
                key = msvcrt.getch()
                if key == b'\x1b': os._exit(0)
                elif key.lower() == b'c': OVERRIDE_TRIGGERED = True
                elif key.lower() == b't':
                    _manual_trade_trigger = True
                    log("\U0001f590\ufe0f Manual trade triggered — entering next qualifying contract at 25% risk.")

            now_et = datetime.now(pytz.timezone("US/Eastern"))
            state = load_state()
            cash = client.get_balance().balance / 100.0
            curr = state.get("current_trade")
            risk_decimal, is_trading_window = get_dynamic_risk(cash)
            current_rsi        = get_btc_rsi()
            current_volatility = get_btc_volatility()

            if OVERRIDE_TRIGGERED:
                log("🛠️ Manual Override: Clearing State")
                state["current_trade"] = None
                save_state(state)
                OVERRIDE_TRIGGERED = False

            if cash <= SAFETY_FLOOR or state.get("strikes", 0) >= STRIKE_LIMIT:
                log(f"🚨 Shutdown: Cash ${cash:.2f} | Strikes {state.get('strikes')}")
                break

            resp = client.get_markets(series_ticker="KXBTC15M", limit=5, status="open")
            markets = [m for m in getattr(resp, 'markets', []) if (m.close_time - now_et).total_seconds() > 0]

            if markets:
                markets.sort(key=lambda x: x.close_time)
                market = markets[0]
                time_left = (market.close_time - now_et).total_seconds() / 60.0
                y_p, n_p = safe_price_cents(market.yes_bid_dollars), safe_price_cents(market.no_bid_dollars)
            else:
                time_left = 0

            # --- MANUAL TRADE TRIGGER (T key) ---
            if _manual_trade_trigger and markets:
                _manual_trade_trigger = False
                # If there's an active trade, sell it first before entering manual
                if curr and curr.get("status") == "filled":
                    log(f"🖐️ Manual: selling existing {curr['side'].upper()} position first...")
                    m_curr = client.get_market(curr['ticker']).market
                    curr_bid = safe_price_cents(m_curr.yes_bid_dollars if curr['side'] == "yes" else m_curr.no_bid_dollars)
                    sell_ok, sell_price, sell_qty = place_order(curr['ticker'], curr['side'], curr['count'], "sell", curr_bid)
                    if sell_ok and sell_price > 0:
                        sell_proceeds = sell_price * sell_qty / 100.0
                        buy_cost      = curr['actual_entry_price'] * curr['count'] / 100.0
                        exit_pnl      = sell_proceeds - buy_cost
                        SESSION_PNL  += exit_pnl
                        update_trades_json({"timestamp": now_et.strftime("%Y-%m-%d %H:%M:%S"), "ticker": curr['ticker'], "side": curr['side'], "pnl": round(exit_pnl, 2), "type": "MANUAL_EXIT", "category": curr.get("category", "bot")})
                        log(f"🖐️ Existing position sold @ {sell_price}c. PnL: ${exit_pnl:+.2f}")
                    else:
                        log("⚠️ Manual: could not sell existing position — market may be closing. Aborting manual entry.")
                        state["current_trade"] = None
                        save_state(state)
                        curr = None
                        time.sleep(1)
                        continue
                    state["current_trade"] = None
                    save_state(state)
                    curr = None
                # Now enter the manual trade
                try:
                    fresh_m = client.get_market(market.ticker).market
                    y_p_m   = safe_price_cents(fresh_m.yes_bid_dollars)
                    n_p_m   = safe_price_cents(fresh_m.no_bid_dollars)
                    tl_m    = (fresh_m.close_time - now_et).total_seconds() / 60.0
                    if 2.0 <= tl_m <= 4.5 and (95 <= y_p_m <= 98 or 95 <= n_p_m <= 98):
                        side_m  = "yes" if 95 <= y_p_m <= 98 else "no"
                        price_m = y_p_m if side_m == "yes" else n_p_m
                        qty_m   = min(int((cash * 0.25) * 100 // price_m), MAX_CONTRACTS)
                        if qty_m >= 1:
                            log(f"🖐️ Manual Trade: {side_m.upper()} @ {price_m}c (Qty: {qty_m}) | 25% risk")
                            success_m, paid_m, filled_m = place_order(market.ticker, side_m, qty_m, "buy", price_m)
                            if success_m and filled_m > 0:
                                state["current_trade"] = {
                                    "ticker": market.ticker, "side": side_m, "count": filled_m,
                                    "entry_price_cents": paid_m, "actual_entry_price": paid_m,
                                    "status": "filled", "category": "manual"
                                }
                                save_state(state)
                                play_sound("buy")
                                log(f"🖐️ Manual filled: {filled_m} contracts @ {paid_m}c")
                            else:
                                log("⚠️ Manual trade failed or zero fill.")
                        else:
                            log("⚠️ Manual trade: insufficient balance for 25% position.")
                    else:
                        log(f"⚠️ Manual trade: no qualifying contract right now (time {tl_m:.1f}m left, Y:{y_p_m}c N:{n_p_m}c).")
                except Exception as e:
                    log(f"⚠️ Manual trade error: {e}")

            # --- MONITORING / STOP LOSS ---
            if curr and curr.get("status") == "filled":
                m_live = client.get_market(curr['ticker']).market
                live_bid = safe_price_cents(m_live.yes_bid_dollars if curr['side'] == "yes" else m_live.no_bid_dollars)
                entry_p = curr['actual_entry_price']
                stop_p = round(entry_p * (1 - STOP_LOSS_THRESHOLD), 2)

                if 0 < live_bid <= stop_p and time_left > 0.5:
                    log(f"🚨 STOP LOSS: Selling {curr['ticker']} (Live: {live_bid}c | SL: {stop_p}c)")
                    state["current_trade"] = None
                    save_state(state)
                    success, actual_sell, filled_qty = place_order(curr['ticker'], curr['side'], curr['count'], "sell", live_bid)
                    if not success or actual_sell == 0:
                        # 409/404 = market already closed/settling — let settlement handle PnL
                        log(f"⚠️ Stop-loss sell rejected (market may have closed) — awaiting settlement.")
                        state["strikes"] = state.get("strikes", 0) + 1
                        save_state(state)
                        play_sound("stop")
                        time.sleep(60)
                        continue
                    # Sanity check: if sell price deviates massively from live_bid, log a warning
                    if abs(actual_sell - live_bid) > 20:
                        log(f"⚠️ Sell fill ({actual_sell}c) deviates from live bid ({live_bid}c) — PnL may be inaccurate.")
                    # PnL: sell proceeds minus buy cost, both in dollars
                    sell_proceeds = actual_sell * filled_qty / 100.0
                    buy_cost      = entry_p * curr['count'] / 100.0
                    pnl = sell_proceeds - buy_cost
                    update_trades_json({"timestamp": now_et.strftime("%Y-%m-%d %H:%M:%S"), "ticker": curr['ticker'], "side": curr['side'], "pnl": round(pnl, 2), "type": "STOP_LOSS"})
                    SESSION_PNL += pnl
                    state["strikes"] = state.get("strikes", 0) + 1
                    save_state(state)
                    play_sound("stop")
                    log(f"💸 Stop-loss complete. PnL: ${pnl:+.2f} | Strikes: {state['strikes']}")
                    log(f"⏸️ Post-SL cooldown (60s) — skipping next entry window.")
                    time.sleep(60)
                    continue

            # --- HEARTBEAT ---
            tier_label = get_balance_tier(cash)["label"]
            vol_flag = " ⚠️VOL" if current_volatility >= VOLATILITY_LIMIT else ""
            # Market prices for heartbeat display
            if markets:
                y_disp = f"{y_p}c" if 0 < y_p <= 99 else "--"
                n_disp = f"{n_p}c" if 0 < n_p <= 99 else "--"
                tl_disp = f"{time_left:.1f}m"
                mkt_str = f" | Y:{y_disp} N:{n_disp} {tl_disp}"
            else:
                mkt_str = " | no market"
            if curr:
                status_text = f" [IN: {curr['side'].upper()} @ {curr.get('actual_entry_price')}c]"
            else:
                status_text = ""
            hb = f"[{now_et.strftime('%H:%M:%S')}] {tier_label} | Risk: {int(risk_decimal*100)}% | RSI: {current_rsi} | Vol: ${current_volatility:.0f}{vol_flag}{mkt_str} | Cash: ${cash:.2f} | Session: ${SESSION_PNL:+.2f}{status_text}"
            print(f"\r{hb:<160}", end="", flush=True)

            if not is_trading_window and not curr:
                time.sleep(10)
                continue
            if not markets:
                time.sleep(5)
                continue

            # --- SETTLEMENT CHECK ---
            if curr and market.ticker != curr["ticker"]:
                log(f"⏳ Finalizing {curr['ticker']}...")
                time.sleep(35)
                res = getattr(client.get_market(curr['ticker']).market, 'result', '').lower()
                if res in ['yes', 'no']:
                    won = (curr['side'] == res)
                    entry_p = curr['actual_entry_price']
                    pnl = (100 - entry_p) * curr['count'] / 100.0 if won else -(entry_p * curr['count'] / 100.0)
                    update_trades_json({"timestamp": now_et.strftime("%Y-%m-%d %H:%M:%S"), "ticker": curr['ticker'], "side": curr['side'], "pnl": round(pnl, 2), "type": "SETTLEMENT", "category": curr.get("category", "bot")})
                    SESSION_PNL += pnl
                    log(f"🏁 RESULT: {res.upper()} | {'WIN' if won else 'LOSS'} | PnL: ${pnl:+.2f}")
                    state["strikes"] = 0 if won else state.get("strikes", 0) + 1
                    state["current_trade"] = None
                    save_state(state)
                    play_sound("settle_win" if won else "settle_loss")

            # --- ENTRY ---
            elif not curr and is_trading_window:
                # Re-fetch fresh prices immediately before entry check
                # to avoid acting on stale quotes from earlier in the tick
                try:
                    fresh = client.get_market(market.ticker).market
                    y_p = safe_price_cents(fresh.yes_bid_dollars)
                    n_p = safe_price_cents(fresh.no_bid_dollars)
                    time_left = (fresh.close_time - now_et).total_seconds() / 60.0
                except Exception:
                    pass  # use existing values if refresh fails

                if 2.0 <= time_left <= 6.0 and (93 <= y_p <= 98 or 93 <= n_p <= 98):
                    side, price = ("yes", y_p) if 93 <= y_p <= 98 else ("no", n_p)

                    rsi_low, rsi_high = get_rsi_limits()
                    if current_volatility >= VOLATILITY_LIMIT:
                        _rsi_stable_ticks = 0
                        if _last_skip_reason != "VOL":
                            log(f"⏭️ Skipping {side.upper()}: volatility ${current_volatility:.0f} exceeds limit ${VOLATILITY_LIMIT}.")
                            _last_skip_reason = "VOL"
                    elif current_rsi < rsi_low:
                        _rsi_stable_ticks = 0
                        if _last_skip_reason != "RSI_LOW":
                            log(f"⏭️ Skipping {side.upper()}: RSI={current_rsi} below {rsi_low} (window limit).")
                            _last_skip_reason = "RSI_LOW"
                    elif current_rsi > rsi_high:
                        _rsi_stable_ticks = 0
                        if _last_skip_reason != "RSI_HIGH":
                            log(f"⏭️ Skipping {side.upper()}: RSI={current_rsi} above {rsi_high} (window limit).")
                            _last_skip_reason = "RSI_HIGH"
                    elif _rsi_stable_ticks < RSI_RECOVERY_TICKS:
                        _rsi_stable_ticks += 1
                        if _last_skip_reason != "RSI_RECOVERY":
                            log(f"⏭️ Skipping {side.upper()}: RSI recovery cooldown ({_rsi_stable_ticks}/{RSI_RECOVERY_TICKS} ticks stable).")
                            _last_skip_reason = "RSI_RECOVERY"
                    else:
                        _last_skip_reason = None
                        qty = int(min(MAX_POSITION_DOLLARS, (cash * risk_decimal)) * 100 // price)
                        qty = min(qty, MAX_CONTRACTS)
                        if qty >= 1 and not _entry_lock:
                            _entry_lock = True
                            log(f"⚡ Pursuit: {side.upper()} @ {price}c (Qty: {qty}) | RSI: {current_rsi}")
                            success, actual_paid, filled_qty = place_order(market.ticker, side, qty, "buy", price)
                            if success and filled_qty > 0:
                                state["current_trade"] = {
                                    "ticker": market.ticker, "side": side, "count": filled_qty,
                                    "entry_price_cents": actual_paid, "actual_entry_price": actual_paid, "status": "filled"
                                }
                                save_state(state)
                                _entry_lock = False
                                play_sound("buy")
                                log(f"✅ Filled: {filled_qty} contracts @ {actual_paid}c")
                                time.sleep(5)
                            else:
                                _entry_lock = False
                                log("⚠️ Entry failed or zero fill. 15s Cooldown...")
                                time.sleep(15)

            time.sleep(1)
        except Exception as e:
            log(f"⚠️ Loop Error: {e}")
            time.sleep(5)