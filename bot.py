import os, json, time, uuid, requests, pytz
from datetime import datetime
from kalshi_python_sync import Configuration, KalshiClient

# Windows-only tools
try:
    import winsound, msvcrt
    HAS_WINDOWS = True
except ImportError:
    HAS_WINDOWS = False

# ====================== CONFIG ======================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(BASE_DIR, "state.json")
TRADES_FILE = os.path.join(BASE_DIR, "trades.json")
LOG_FILE = os.path.join(BASE_DIR, "log.txt")

MAX_SLIPPAGE = 2 
MAX_POSITION_DOLLARS = 500.0
SAFETY_FLOOR = 1000.0
STRIKE_LIMIT = 3
STOP_LOSS_THRESHOLD = 0.40 
SESSION_PNL = 0.00         

# --- RSI ---
RSI_PERIOD = 9        
RSI_CRASH_LIMIT = 30  # Filter YES if RSI is too low (bearish)
RSI_SURGE_LIMIT = 70  # Filter NO if RSI is too high (bullish)

# ====================== HELPERS ======================
def log(msg: str):
    ts = datetime.now(pytz.timezone("US/Eastern")).strftime("%Y-%m-%d %H:%M:%S ET")
    print(f"\n[{ts}] {msg}")
    with open(LOG_FILE, "a", encoding="utf-8") as f: f.write(f"[{ts}] {msg}\n")

def load_state():
    default = {"strikes": 0, "current_trade": None, "pending_order": False}
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            try: return {**default, **json.load(f)}
            except: pass
    return default

def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f: json.dump(state, f, indent=2)

def update_trades_json(trade_entry):
    trades = []
    trade_entry["category"] = "bot"
    try:
        if os.path.exists(TRADES_FILE):
            with open(TRADES_FILE, "r") as f:
                try: trades = json.load(f)
                except: trades = []
        trades.append(trade_entry)
        with open(TRADES_FILE, "w") as f: json.dump(trades, f, indent=2)
    except Exception as e: log(f"⚠️ JSON Error: {e}")

def safe_price_cents(value) -> int:
    try: return int(round(float(value or 0) * 100))
    except: return 0

# ====================== CORE ENGINE ======================
def place_order(client, ticker, side, count, action, price_cents):
    try:
        order_id = str(uuid.uuid4())
        limit = min(99, price_cents + MAX_SLIPPAGE) if action == "buy" else max(1, price_cents - MAX_SLIPPAGE)
        resp = client.create_order(ticker=ticker, side=side, action=action, count=count, type="limit", 
                                   client_order_id=order_id, yes_price=limit if side=="yes" else None, 
                                   no_price=limit if side=="no" else None)
        ex_id = resp.order.order_id 
        for _ in range(10):
            time.sleep(1.5)
            order = client.get_order(ex_id).order
            qty = getattr(order, 'filled', 0)
            if qty > 0:
                avg = getattr(order, 'avg_fill_price', 0)
                if avg == 0: log("⚠️ Warning: avg_fill_price is 0. Using estimate.")
                return True, (avg if avg > 0 else price_cents), qty
        return False, 0, 0
    except Exception as e: log(f"❌ Order Error: {e}"); return False, 0, 0

# ====================== MAIN ======================
if __name__ == "__main__":
    log("🪄 Magick Bot v5.2.15 Active (The Concrete Sentinel)")
    # Init API... (Assuming client setup from previous)
    
    while True:
        state = load_state()
        now_et = datetime.now(pytz.timezone("US/Eastern"))
        
        # 1. PORTFOLIO SYNC (Recovery)
        try:
            pos_list = client.get_portfolio().positions
            active_pos = next((p for p in pos_list if "KXBTC" in p.ticker and p.position != 0), None)
            if active_pos and not state["current_trade"]:
                log(f"🔄 Recovery: Adopting {active_pos.position} contracts.")
                state["current_trade"] = {"ticker": active_pos.ticker, "side": "yes" if active_pos.position > 0 else "no", 
                                          "count": abs(active_pos.position), "actual_entry_price": 0}
                state["pending_order"] = False
                save_state(state)
        except: pass

        curr = state["current_trade"]

        # 2. DECOUPLED STOP LOSS
        if curr:
            try:
                m_live = client.get_market(curr['ticker']).market
                # If market is still open, check stop loss
                if m_live.status == "open":
                    live_bid = safe_price_cents(m_live.yes_bid_dollars if curr['side'] == "yes" else m_live.no_bid_dollars)
                    if curr['actual_entry_price'] > 0:
                        sl_price = curr['actual_entry_price'] * (1 - STOP_LOSS_THRESHOLD)
                        if 0 < live_bid <= sl_price:
                            log(f"🚨 STOP LOSS TRIGGERED @ {live_bid}c")
                            success, _, _ = place_order(client, curr['ticker'], curr['side'], curr['count'], "sell", live_bid)
                            if success:
                                pnl = (live_bid - curr['actual_entry_price']) * curr['count'] / 100.0
                                update_trades_json({"timestamp": now_et.isoformat(), "pnl": round(pnl, 2), "type": "STOP_LOSS"})
                                state["current_trade"] = None
                                save_state(state)
                                continue
                
                # 3. ROBUST SETTLEMENT (Retry Loop)
                elif m_live.status in ["closed", "settled"]:
                    log(f"⏳ Waiting for Settlement on {curr['ticker']}...")
                    for _ in range(30): # Try for 5 minutes
                        m_check = client.get_market(curr['ticker']).market
                        res = getattr(m_check, 'result', '').lower()
                        if res in ['yes', 'no']:
                            won = (curr['side'] == res)
                            pnl = (100 - curr['actual_entry_price']) * curr['count'] / 100.0 if won else -(curr['actual_entry_price'] * curr['count'] / 100.0)
                            log(f"🏁 SETTLED: {res.upper()} | PnL: ${pnl:+.2f}")
                            update_trades_json({"timestamp": now_et.isoformat(), "ticker": curr['ticker'], "pnl": round(pnl, 2), "type": "SETTLE"})
                            state["current_trade"] = None
                            save_state(state)
                            break
                        time.sleep(10)
            except Exception as e: log(f"⚠️ Monitor Error: {e}")

        # 4. ENTRY LOGIC (Only if not pending and no active trade)
        elif not state["pending_order"]:
            # (Entry logic from v5.2.14 goes here, but ensures PENDING_ORDER is saved to state immediately)
            pass

        time.sleep(1)