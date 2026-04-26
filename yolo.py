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
BASE_DIR           = os.path.dirname(os.path.abspath(__file__))
APIKEY_FILE        = os.path.join(BASE_DIR, "apikey.txt")
PRIVATE_FILE       = os.path.join(BASE_DIR, "private.txt")
LOG_FILE           = os.path.join(BASE_DIR, "log.txt")          # shared with main bot
STATE_FILE         = os.path.join(BASE_DIR, "yolostate.json")   # separate state
TRADE_HISTORY_FILE = os.path.join(BASE_DIR, "trades.json")      # shared with main bot (same format)
YOLO_LOG_FILE      = os.path.join(BASE_DIR, "yolologs.json")    # yolo-only detail log

# --- Position ---
FIXED_POSITION_DOLLARS = 250.0   # fixed dollar size per trade
SAFETY_FLOOR           = 800     # shutdown floor
STRIKE_LIMIT           = 3       # stops before shutdown
STOP_LOSS_THRESHOLD    = 0.40    # 40% stop

# --- Entry window ---
TIME_WINDOW_MAX = 4.0   # minutes before expiry — entry opens
TIME_WINDOW_MIN = 1.5   # minutes before expiry — entry closes
ENTRY_PRICES    = (97, 98)  # only 97c or 98c

# --- Auto-arm guards (smart trigger) ---
# YOLO auto-arms when ALL conditions are met:
#   RSI between RSI_ARM_LOW and RSI_ARM_HIGH
#   Volatility under VOL_ARM_LIMIT
# Auto-disarms immediately when any condition breaks.
RSI_ARM_LOW    = 35    # don't arm if RSI oversold
RSI_ARM_HIGH   = 70    # don't arm if RSI overbought
VOL_ARM_LIMIT  = 200   # don't arm if BTC volatile (stricter than main bot's $300)

# --- Schedule (mirrors main bot exactly) ---
# Blocked windows: pre-market and evening — same dangerous windows that caused
# the -$226 stop on Apr 24 at 8:58AM. Neither RSI nor vol guards catch these.
# Format: (start_hhmm, end_hhmm) in ET — bot is SILENT during these windows.
BLOCKED_WINDOWS = [
    (500,  830),   # Pre-market:  5:00AM – 8:30AM  (thin liquidity, news risk)
    (1730, 2200),  # Evening:     5:30PM – 10:00PM  (end-of-day volatility)
]

def is_blocked_window(now_et: datetime) -> bool:
    ti = int(now_et.strftime('%H%M'))
    for start, end in BLOCKED_WINDOWS:
        if start <= ti < end:
            return True
    return False

MAX_SLIPPAGE = 2

# --- RSI ---
RSI_PERIOD = 9
VOLATILITY_CANDLES = 5

# ====================== HELPERS ======================
def log(msg: str):
    ts = datetime.now(pytz.timezone("US/Eastern")).strftime("%Y-%m-%d %H:%M:%S ET")
    line = f"[{ts}] {msg}"
    print(f"\n{line}")
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            try: return json.load(f)
            except: pass
    return {"strikes": 0, "current_trade": None}

def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)

def log_trade(trade_data):
    now_et = datetime.now(pytz.timezone("US/Eastern"))
    ts     = now_et.strftime("%Y-%m-%d %H:%M:%S")

    # 1) Write to shared trades.json in exact same format as main bot
    shared_record = {
        "timestamp": ts,
        "ticker":    trade_data["ticker"],
        "side":      trade_data["side"],
        "pnl":       trade_data["pnl"],
        "type":      trade_data["type"],
    }
    shared = []
    if os.path.exists(TRADE_HISTORY_FILE):
        with open(TRADE_HISTORY_FILE, "r", encoding="utf-8") as f:
            try: shared = json.load(f)
            except: shared = []
    shared.append(shared_record)
    with open(TRADE_HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(shared, f, indent=2)

    # 2) Write to yolologs.json with full detail
    detail_record = {**shared_record, **trade_data, "timestamp": ts}
    detail = []
    if os.path.exists(YOLO_LOG_FILE):
        with open(YOLO_LOG_FILE, "r", encoding="utf-8") as f:
            try: detail = json.load(f)
            except: detail = []
    detail.append(detail_record)
    with open(YOLO_LOG_FILE, "w", encoding="utf-8") as f:
        json.dump(detail, f, indent=2)

def safe_price_cents(value) -> int:
    try: return int(round(float(value or 0) * 100))
    except: return 0

def play_sound(event_type):
    if not HAS_WINDOWS: return
    sounds = {
        "arm":        [(1800, 100), (2200, 100)],
        "disarm":     [(800, 150)],
        "buy":        [(2000, 200)],
        "settle_win": [(2500, 200), (3000, 200)],
        "settle_loss":[(600, 500)],
        "stop":       [(400, 1000)],
    }
    for f, d in sounds.get(event_type, []):
        winsound.Beep(f, d)

def parse_order(order) -> tuple:
    try:
        qty = int(float(getattr(order, 'fill_count_fp', '0') or '0'))
        if qty <= 0: return 0, 0
        taker = float(getattr(order, 'taker_fill_cost_dollars', '0') or '0')
        maker = float(getattr(order, 'maker_fill_cost_dollars', '0') or '0')
        cost  = taker if taker > 0 else maker
        avg_cents = int(round((cost / qty) * 100)) if cost > 0 else 0
        return qty, avg_cents
    except: return 0, 0

def get_btc_rsi() -> float:
    try:
        url  = f"https://api-pub.bitfinex.com/v2/candles/trade:1m:tBTCUSD/hist?limit={RSI_PERIOD + 10}"
        resp = requests.get(url, timeout=5).json()
        closes = [c[2] for c in resp][::-1]
        deltas = [closes[i+1] - closes[i] for i in range(len(closes)-1)]
        gains  = [d if d > 0 else 0 for d in deltas]
        losses = [-d if d < 0 else 0 for d in deltas]
        avg_gain = sum(gains[-RSI_PERIOD:]) / RSI_PERIOD
        avg_loss = sum(losses[-RSI_PERIOD:]) / RSI_PERIOD
        if avg_loss == 0: return 100.0
        rs = avg_gain / avg_loss
        return round(100 - (100 / (1 + rs)), 1)
    except: return 50.0

def get_btc_volatility() -> float:
    try:
        url  = f"https://api-pub.bitfinex.com/v2/candles/trade:1m:tBTCUSD/hist?limit={VOLATILITY_CANDLES + 2}"
        resp = requests.get(url, timeout=5).json()
        candles = resp[:VOLATILITY_CANDLES]
        highs = [c[3] for c in candles]
        lows  = [c[4] for c in candles]
        return round(max(highs) - min(lows), 2)
    except: return 0.0

# ====================== API SETUP ======================
with open(APIKEY_FILE, "r", encoding="utf-8") as f: api_key_id = f.read().strip()
with open(PRIVATE_FILE, "r", encoding="utf-8") as f: private_key_pem = f.read()

config = Configuration(host="https://api.elections.kalshi.com/trade-api/v2")
config.api_key_id = api_key_id
config.private_key_pem = private_key_pem
client = KalshiClient(config)

def place_order(ticker, side, count, action, price_cents=None):
    try:
        order_id    = str(uuid.uuid4())
        actual_limit = min(99, price_cents + MAX_SLIPPAGE) if action == "buy" else max(1, price_cents - MAX_SLIPPAGE)
        resp = client.create_order(
            ticker=ticker, side=side, action=action, count=count, type="limit",
            client_order_id=order_id,
            yes_price=actual_limit if side == "yes" else None,
            no_price =actual_limit if side == "no"  else None
        )
        order     = resp.order
        target_id = order.order_id
        qty, avg_cents = parse_order(order)
        if qty > 0: return True, avg_cents, qty
        for _ in range(5):
            time.sleep(1.5)
            order_info = client.get_order(target_id).order
            qty, avg_cents = parse_order(order_info)
            if qty > 0: return True, avg_cents, qty
        return False, 0, 0
    except Exception as e:
        log(f"❌ Order Error: {e}")
        return False, 0, 0

# ====================== MAIN LOOP ======================
if __name__ == "__main__":
    log("🎯 YOLO Bot v2.0 Active | Auto-arm: RSI 35-70 | Vol < $200 | Entry: 97-98c | 4.0-1.5m")
    log("   Press T = manual fire | C = clear state | ESC = quit")

    SESSION_PNL         = 0.0
    _entry_lock         = False
    _armed              = False          # auto-arm state
    _manual_triggered   = False
    _override_triggered = False
    _last_arm_log       = None

    while True:
        try:
            # --- KEYBOARD ---
            if HAS_WINDOWS and msvcrt.kbhit():
                key = msvcrt.getch()
                if key == b'\x1b':
                    log("👋 Exiting YOLO Bot.")
                    break
                elif key.lower() == b'c':
                    _override_triggered = True
                elif key.lower() == b't':
                    _manual_triggered = True
                    log("🔑 Manual trigger queued — will fire next valid window.")

            now_et = datetime.now(pytz.timezone("US/Eastern"))
            state  = load_state()
            curr   = state.get("current_trade")
            cash   = client.get_balance().balance / 100.0

            # --- SAFETY CHECKS ---
            if _override_triggered:
                log("🛠️ Manual Override: Clearing State")
                state["current_trade"] = None
                save_state(state)
                _override_triggered = False

            if cash <= SAFETY_FLOOR or state.get("strikes", 0) >= STRIKE_LIMIT:
                log(f"🚨 Shutdown: Cash ${cash:.2f} | Strikes {state.get('strikes')}")
                break

            # --- MARKET DATA ---
            resp    = client.get_markets(series_ticker="KXBTC15M", limit=5, status="open")
            markets = [m for m in getattr(resp, 'markets', []) if (m.close_time - now_et).total_seconds() > 0]

            if markets:
                markets.sort(key=lambda x: x.close_time)
                market    = markets[0]
                time_left = (market.close_time - now_et).total_seconds() / 60.0
                y_p = safe_price_cents(market.yes_bid_dollars)
                n_p = safe_price_cents(market.no_bid_dollars)
            else:
                time_left = 0

            # --- RSI & VOLATILITY ---
            current_rsi = get_btc_rsi()
            current_vol = get_btc_volatility()

            # --- SCHEDULE CHECK ---
            blocked = is_blocked_window(now_et)
            if blocked:
                if _armed:
                    _armed = False
                    if _last_arm_log != "BLOCKED":
                        log(f"🚫 SCHEDULE BLOCK — window disabled (pre-market or evening). Disarming.")
                        play_sound("disarm")
                        _last_arm_log = "BLOCKED"
                if _manual_triggered:
                    log(f"🚫 Manual trigger cancelled — blocked window.")
                    _manual_triggered = False
                print(f"\r[{now_et.strftime('%H:%M:%S')}] 🚫BLOCKED | Cash: ${cash:.2f} | Session: ${SESSION_PNL:+.2f}{' '*40}", end="", flush=True)
                time.sleep(5)
                continue

            # --- AUTO-ARM LOGIC ---
            conditions_met = (
                RSI_ARM_LOW <= current_rsi <= RSI_ARM_HIGH
                and current_vol < VOL_ARM_LIMIT
            )

            if conditions_met and not _armed:
                _armed = True
                if _last_arm_log != "ARMED":
                    log(f"🟢 AUTO-ARMED | RSI {current_rsi} | Vol ${current_vol:.0f}")
                    play_sound("arm")
                    _last_arm_log = "ARMED"
            elif not conditions_met and _armed:
                _armed = False
                disarm_reason = f"RSI {current_rsi}" if not (RSI_ARM_LOW <= current_rsi <= RSI_ARM_HIGH) else f"Vol ${current_vol:.0f}"
                if _last_arm_log != "DISARMED":
                    log(f"🔴 AUTO-DISARMED | {disarm_reason}")
                    play_sound("disarm")
                    _last_arm_log = "DISARMED"
                _manual_triggered = False  # cancel pending manual trigger if disarmed

            # --- STOP LOSS ---
            if curr and curr.get("status") == "filled":
                try:
                    m_live   = client.get_market(curr['ticker']).market
                    live_bid = safe_price_cents(m_live.yes_bid_dollars if curr['side'] == "yes" else m_live.no_bid_dollars)
                    entry_p  = curr['actual_entry_price']
                    stop_p   = round(entry_p * (1 - STOP_LOSS_THRESHOLD), 2)

                    if 0 < live_bid <= stop_p and time_left > 0.5:
                        log(f"🚨 STOP LOSS: Selling {curr['ticker']} (Live: {live_bid}c | SL: {stop_p}c)")
                        success, actual_sell, filled_qty = place_order(curr['ticker'], curr['side'], curr['count'], "sell", live_bid)
                        if success:
                            pnl = (actual_sell * filled_qty / 100.0) - (entry_p * curr['count'] / 100.0)
                            SESSION_PNL += pnl
                            log_trade({"ticker": curr['ticker'], "side": curr['side'], "pnl": round(pnl, 2), "type": "STOP_LOSS"})
                            state["strikes"] = state.get("strikes", 0) + 1
                            state["current_trade"] = None
                            save_state(state)
                            play_sound("stop")
                            log(f"💸 Stop-loss complete. PnL: ${pnl:+.2f} | Strikes: {state['strikes']}")
                            time.sleep(10)
                            continue
                except: pass

            # --- HEARTBEAT ---
            arm_str  = "🟢ARMED" if _armed else "🔴DISARMED"
            mkt_str  = f"Y:{y_p}c N:{n_p}c {time_left:.1f}m" if markets else "no market"
            pos_str  = f" [IN: {curr['side'].upper()} @ {curr.get('actual_entry_price')}c]" if curr else ""
            vol_flag = " ⚠️VOL" if current_vol >= VOL_ARM_LIMIT else ""
            hb = (f"[{now_et.strftime('%H:%M:%S')}] {arm_str} | RSI: {current_rsi} | "
                  f"Vol: ${current_vol:.0f}{vol_flag} | {mkt_str} | "
                  f"Cash: ${cash:.2f} | Session: ${SESSION_PNL:+.2f}{pos_str}")
            print(f"\r{hb:<160}", end="", flush=True)

            if not markets:
                time.sleep(5)
                continue

            # --- SETTLEMENT ---
            if curr and market.ticker != curr["ticker"]:
                if not curr.get("finalizing_since"):
                    curr["finalizing_since"] = now_et.timestamp()
                    save_state(state)

                waited_minutes = (now_et.timestamp() - curr["finalizing_since"]) / 60.0

                if waited_minutes > 10:
                    log(f"⚠️ Settlement timeout after {waited_minutes:.0f}m — {curr['ticker']} never resolved (CF Benchmarks outage?). Clearing state. Check Kalshi manually.")
                    state["current_trade"] = None
                    save_state(state)
                else:
                    log(f"⏳ Finalizing {curr['ticker']}... ({waited_minutes:.0f}m elapsed)")
                    time.sleep(35)
                    res = getattr(client.get_market(curr['ticker']).market, 'result', '').lower()
                    if res in ['yes', 'no']:
                        won   = (curr['side'] == res)
                        entry_p = curr['actual_entry_price']
                        pnl   = (100 - entry_p) * curr['count'] / 100.0 if won else -(entry_p * curr['count'] / 100.0)
                        SESSION_PNL += pnl
                        log_trade({"ticker": curr['ticker'], "side": curr['side'], "pnl": round(pnl, 2), "type": "SETTLEMENT"})
                        log(f"🏁 RESULT: {res.upper()} | {'WIN ✅' if won else 'LOSS ❌'} | PnL: ${pnl:+.2f} | Session: ${SESSION_PNL:+.2f}")
                        state["strikes"] = 0 if won else state.get("strikes", 0) + 1
                        state["current_trade"] = None
                        save_state(state)
                        play_sound("settle_win" if won else "settle_loss")

            # --- ENTRY: auto or manual ---
            elif not curr and not _entry_lock:
                in_window = TIME_WINDOW_MIN <= time_left <= TIME_WINDOW_MAX
                price_ok  = any(p in ENTRY_PRICES for p in [y_p, n_p])

                # Determine trigger: armed (auto) or manual keypress
                should_fire = in_window and price_ok and (_armed or _manual_triggered)

                if should_fire:
                    # Re-fetch fresh price
                    try:
                        fresh = client.get_market(market.ticker).market
                        y_p   = safe_price_cents(fresh.yes_bid_dollars)
                        n_p   = safe_price_cents(fresh.no_bid_dollars)
                        time_left = (fresh.close_time - now_et).total_seconds() / 60.0
                    except: pass

                    if y_p in ENTRY_PRICES:
                        side, price = "yes", y_p
                    elif n_p in ENTRY_PRICES:
                        side, price = "no", n_p
                    else:
                        # Price slipped out of range
                        if _manual_triggered:
                            log(f"⚠️ Manual trigger: price slipped ({y_p}c/{n_p}c) — no entry.")
                            _manual_triggered = False
                        time.sleep(1)
                        continue

                    qty = int((FIXED_POSITION_DOLLARS * 100) // price)
                    if qty >= 1:
                        trigger_label = "MANUAL" if _manual_triggered else "AUTO"
                        _entry_lock       = True
                        _manual_triggered = False
                        log(f"⚡ YOLO [{trigger_label}]: {side.upper()} @ {price}c | Qty: {qty} | Time: {time_left:.1f}m | RSI: {current_rsi} | Vol: ${current_vol:.0f}")
                        success, actual_paid, filled_qty = place_order(market.ticker, side, qty, "buy", price)
                        if success and filled_qty > 0:
                            state["current_trade"] = {
                                "ticker": market.ticker, "side": side,
                                "count": filled_qty, "actual_entry_price": actual_paid,
                                "status": "filled"
                            }
                            save_state(state)
                            play_sound("buy")
                            log(f"✅ Filled: {filled_qty} @ {actual_paid}c")
                        else:
                            log("⚠️ Entry failed or zero fill.")
                        _entry_lock = False

                elif _manual_triggered and time_left < TIME_WINDOW_MIN:
                    log(f"⚠️ Window closed. Manual trigger cancelled.")
                    _manual_triggered = False

            time.sleep(1)

        except Exception as e:
            log(f"⚠️ Loop Error: {e}")
            time.sleep(5)