import os
import json
import time
import uuid
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

# SECURITY CONSTANTS
MAX_SLIPPAGE = 2 
MAX_POSITION_DOLLARS = 500.0 # Increased to accommodate 15% allocations
SAFETY_FLOOR = 600.0
STRIKE_LIMIT = 3
LAST_ORDER_EXIT_TIME = 0 
OVERRIDE_TRIGGERED = False
TEST_MODE_TRIGGERED = False
SESSION_PNL = 0.0

# ====================== DYNAMIC RISK ENGINE ======================
def get_dynamic_risk():
    """Returns (risk_decimal, is_trading_hour) based on ET Schedule"""
    tz = pytz.timezone("US/Eastern")
    now = datetime.now(tz)
    day = now.weekday() # 0=Mon, 6=Sun
    hour = now.hour
    minute = now.minute
    time_float = hour + (minute / 60.0)

    # SATURDAY: No Trading
    if day == 5:
        return 0.0, False

    # SUNDAY
    if day == 6:
        if 12.0 <= time_float < 17.0: return 0.05, True
        return 0.0, False

    # MONDAY - FRIDAY
    # 2:00am to 5:00am: 15%
    if 2.0 <= time_float < 5.0: return 0.15, True
    # 5:00am to 8:30am: 5%
    if 5.0 <= time_float < 8.5: return 0.05, True
    # 10:30am to 12:00pm: 15%
    if 10.5 <= time_float < 12.0: return 0.15, True
    # 12:00pm to 4:00pm: 10%
    if 12.0 <= time_float < 16.0: return 0.10, True
    # 4:30pm to 5:30pm: 15%
    if 16.5 <= time_float < 17.5: return 0.15, True
    # 10:00pm to 12:00am: 10%
    if 22.0 <= time_float < 24.0: return 0.10, True

    return 0.0, False

# ====================== STARTUP ======================
with open(APIKEY_FILE, "r", encoding="utf-8") as f: api_key_id = f.read().strip()
with open(PRIVATE_FILE, "r", encoding="utf-8") as f: private_key_pem = f.read()

config = Configuration(host="https://api.elections.kalshi.com/trade-api/v2")
config.api_key_id = api_key_id
config.private_key_pem = private_key_pem
client = KalshiClient(config)

# ====================== HELPERS ======================
def save_trade_to_history(trade_data):
    history = []
    if os.path.exists(TRADES_FILE):
        with open(TRADES_FILE, "r", encoding="utf-8") as f:
            try: history = json.load(f)
            except: history = []
    history.append(trade_data)
    with open(TRADES_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, indent=2)

def play_sound(event_type):
    if not HAS_WINDOWS: return
    s = {"buy":[(2000,200)], "settle_win":[(2500,200),(3000,200)], "settle_loss":[(600,500)]}
    for f, d in s.get(event_type, []): winsound.Beep(f, d)

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

def check_keys():
    global OVERRIDE_TRIGGERED, TEST_MODE_TRIGGERED
    if HAS_WINDOWS and msvcrt.kbhit():
        key = msvcrt.getch()
        if key == b'\x1b': os._exit(0) # ESC
        elif key.lower() == b'c': OVERRIDE_TRIGGERED = True
        elif key == b'1': TEST_MODE_TRIGGERED = True

def safe_price_cents(value) -> int:
    try: return int(round(float(value or 0) * 100))
    except: return 0

# ====================== EXECUTION ======================
def place_order(ticker, side, count, action, price_cents=None, is_test=False):
    global LAST_ORDER_EXIT_TIME
    try:
        pre_bal = client.get_balance().balance / 100.0
        order_id = str(uuid.uuid4())
        slip = 5 if is_test else MAX_SLIPPAGE
        ceiling = 100 if is_test else 98
        actual_price = min(ceiling, price_cents + slip) if action == "buy" else max(1, price_cents - slip)
        
        client.create_order(ticker=ticker, side=side, action=action, count=count, type="limit", 
                            client_order_id=order_id, yes_price=actual_price if side=="yes" else None, 
                            no_price=actual_price if side=="no" else None)
        
        for _ in range(3):
            time.sleep(1)
            if (pre_bal - (client.get_balance().balance / 100.0)) >= 0.01: return True 
        try: client.cancel_order(order_id)
        except: pass
        LAST_ORDER_EXIT_TIME = time.time()
        return False
    except Exception as e:
        log(f"❌ Error: {e}"); return False

# ====================== MAIN LOOP ======================
if __name__ == "__main__":
    log("🪄 Magick Bot v5.0.9 Starting (Strategic Allocation Mode)")
    st = load_state(); st["current_trade"] = None; save_state(st)
    
    while True:
        try:
            check_keys()
            now_et = datetime.now(pytz.timezone("US/Eastern"))
            state = load_state()
            cash = client.get_balance().balance / 100.0
            strikes = state.get("strikes", 0)
            
            # Risk Logic
            risk_decimal, is_trading_window = get_dynamic_risk()

            if OVERRIDE_TRIGGERED:
                log("🛠️ Manual Override: Clearing State")
                state["current_trade"] = None; save_state(state); OVERRIDE_TRIGGERED = False

            if cash <= SAFETY_FLOOR or strikes >= STRIKE_LIMIT:
                log(f"🚨 Shutdown: Cash ${cash:.2f} | Strikes {strikes}"); break
                
            if time.time() - LAST_ORDER_EXIT_TIME < 5:
                time.sleep(1); continue

            # --- HEARTBEAT ---
            risk_label = f"{int(risk_decimal*100)}%" if is_trading_window else "DORMANT"
            status_text = ""
            curr = state.get("current_trade")
            if curr:
                status_text = f" [IN TRADE: {curr['side'].upper()}]" if curr.get('status') != 'pending' else " [PENDING...]"
            
            # Simplified ticker fetch for heartbeat
            print(f"\r[{now_et.strftime('%H:%M:%S')}] Risk: {risk_label} | Cash: ${cash:.2f} | Session: ${SESSION_PNL:+.2f}{status_text}", end="")

            if not is_trading_window and not curr:
                time.sleep(10); continue

            resp = client.get_markets(series_ticker="KXBTC15M", limit=5, status="open")
            markets = [m for m in getattr(resp, 'markets', []) if (m.close_time - now_et).total_seconds() > 0]
            if not markets: time.sleep(5); continue
            markets.sort(key=lambda x: x.close_time); market = markets[0]

            y_p = safe_price_cents(market.yes_bid_dollars)
            n_p = safe_price_cents(market.no_bid_dollars)
            time_left = (market.close_time - now_et).total_seconds() / 60.0

            # 1. SETTLEMENT
            if curr and isinstance(curr, dict) and curr.get("status") != "pending" and market.ticker != curr["ticker"]:
                log(f"⏳ Finalizing {curr['ticker']}...")
                time.sleep(35) 
                m_info = client.get_market(curr['ticker']).market
                res = getattr(m_info, 'result', '').lower()
                if res in ['yes', 'no']:
                    won = (curr['side'] == res)
                    pnl = (100 - curr['entry_price_cents']) * curr['count'] / 100.0 if won else - (curr['entry_price_cents'] * curr['count'] / 100.0)
                    SESSION_PNL += pnl
                    log(f"🏁 RESULT: {res.upper()} | {'WIN' if won else 'LOSS'} | PnL: ${pnl:+.2f}")
                    save_trade_to_history({
                        "timestamp": now_et.strftime("%Y-%m-%d %H:%M:%S"), "ticker": curr["ticker"],
                        "side": curr["side"], "entry": curr["entry_price_cents"], "exit": 100 if won else 0,
                        "qty": curr["count"], "pnl": round(pnl, 2), "result": "WIN" if won else "LOSS"
                    })
                    state["strikes"] = 0 if won else strikes + 1
                    state["current_trade"] = None; save_state(state)
                    if won: play_sound("settle_win")
                    else: play_sound("settle_loss")

            # 2. ENTRY (Only if in Window)
            elif not curr and is_trading_window:
                if 1.5 <= time_left <= 6.0 and (94 <= y_p <= 98 or 94 <= n_p <= 98):
                    side, price = ("yes", y_p) if 94 <= y_p <= 98 else ("no", n_p)
                    qty = int(min(MAX_POSITION_DOLLARS, (cash * risk_decimal)) * 100 // price)
                    if qty >= 1:
                        state["current_trade"] = {"ticker": market.ticker, "status": "pending"}; save_state(state)
                        log(f"⚡ Pursuit: {side.upper()} @ {price}c (Risk: {risk_label}, Qty: {qty})")
                        if place_order(market.ticker, side, qty, "buy", price):
                            state["current_trade"] = {"ticker": market.ticker, "side": side, "count": qty, "entry_price_cents": price}
                            save_state(state); play_sound("buy")
                        else: state["current_trade"] = None; save_state(state)

            time.sleep(1) 
        except Exception as e: time.sleep(5)