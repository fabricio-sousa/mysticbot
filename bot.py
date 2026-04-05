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
MAX_POSITION_DOLLARS = 500.0
SAFETY_FLOOR = 1000.0
STRIKE_LIMIT = 3
STOP_LOSS_THRESHOLD = 0.40
OVERRIDE_TRIGGERED = False
SESSION_PNL = 0.00

# --- RSI ---
RSI_PERIOD      = 9
RSI_CRASH_LIMIT = 30   # Skip YES entry if RSI too low (bearish momentum)
RSI_SURGE_LIMIT = 70   # Skip NO  entry if RSI too high (bullish momentum)

# ====================== DYNAMIC RISK ENGINE ======================
def get_dynamic_risk():
    tz = pytz.timezone("US/Eastern")
    now = datetime.now(tz)
    day = now.weekday()
    hour = now.hour
    minute = now.minute
    time_float = hour + (minute / 60.0)

    if 0 <= day <= 4:
        if  0.0 <= time_float <  5.0: return 0.05, True   # Safe overnights
        if  5.0 <= time_float <  8.5: return 0.05, True   # Low priority pre-market
        if 10.5 <= time_float < 12.0: return 0.15, True   # High confidence open
        if 12.0 <= time_float < 16.0: return 0.10, True   # Balanced midday
        if 16.5 <= time_float < 17.5: return 0.15, True   # Primary close window
        if 22.0 <= time_float < 24.0: return 0.05, True   # Asian open
    elif day == 6:
        if 12.0 <= time_float < 17.0: return 0.05, True   # Sunday

    return 0.01, True   # Standby

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
        return 50.0   # neutral fallback

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

        for _ in range(5):
            time.sleep(1.5)
            order_info = client.get_order(resp.order_id).order
            if order_info.status == 'filled' or order_info.filled_count > 0:
                return True, order_info.avg_fill_price, order_info.filled_count
            if order_info.status in ['canceled', 'expired']:
                break
        return False, 0, 0
    except Exception as e:
        log(f"❌ Order Error: {e}")
        return False, 0, 0

# ====================== MAIN LOOP ======================
if __name__ == "__main__":
    log("🪄 Magick Bot v5.2.6 Active (RSI Filter)")

    while True:
        try:
            if HAS_WINDOWS and msvcrt.kbhit():
                key = msvcrt.getch()
                if key == b'\x1b': os._exit(0)
                elif key.lower() == b'c': OVERRIDE_TRIGGERED = True

            now_et = datetime.now(pytz.timezone("US/Eastern"))
            state = load_state()
            cash = client.get_balance().balance / 100.0
            curr = state.get("current_trade")
            risk_decimal, is_trading_window = get_dynamic_risk()
            current_rsi = get_btc_rsi()

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

            # --- MONITORING / STOP LOSS ---
            if curr and curr.get("status") == "filled":
                m_live = client.get_market(curr['ticker']).market
                live_bid = safe_price_cents(m_live.yes_bid_dollars if curr['side'] == "yes" else m_live.no_bid_dollars)
                entry_p = curr['actual_entry_price']
                stop_p = entry_p * (1 - STOP_LOSS_THRESHOLD)

                if 0 < live_bid <= stop_p and time_left > 0.5:
                    log(f"🚨 STOP LOSS: Selling {curr['ticker']} (Live: {live_bid}c | SL: {stop_p}c)")
                    success, _, _ = place_order(curr['ticker'], curr['side'], curr['count'], "sell", live_bid)
                    if success:
                        pnl = (live_bid - entry_p) * curr['count'] / 100.0
                        update_trades_json({"timestamp": now_et.strftime("%Y-%m-%d %H:%M:%S"), "ticker": curr['ticker'], "side": curr['side'], "pnl": round(pnl, 2), "type": "STOP_LOSS"})
                        SESSION_PNL += pnl
                        state["current_trade"] = None
                        state["strikes"] = state.get("strikes", 0) + 1
                        save_state(state)
                        play_sound("stop")
                        continue

            # --- HEARTBEAT ---
            status_text = f" [IN: {curr['side'].upper()} @ {curr.get('actual_entry_price')}c]" if curr else ""
            print(f"\r[{now_et.strftime('%H:%M:%S')}] Risk: {int(risk_decimal*100)}% | RSI: {current_rsi} | Cash: ${cash:.2f} | Session: ${SESSION_PNL:+.2f}{status_text}", end="")

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
                    update_trades_json({"timestamp": now_et.strftime("%Y-%m-%d %H:%M:%S"), "ticker": curr['ticker'], "side": curr['side'], "pnl": round(pnl, 2), "type": "SETTLEMENT"})
                    SESSION_PNL += pnl
                    log(f"🏁 RESULT: {res.upper()} | {'WIN' if won else 'LOSS'} | PnL: ${pnl:+.2f}")
                    state["strikes"] = 0 if won else state.get("strikes", 0) + 1
                    state["current_trade"] = None
                    save_state(state)
                    play_sound("settle_win" if won else "settle_loss")

            # --- ENTRY ---
            elif not curr and is_trading_window:
                if 2.0 <= time_left <= 6.0 and (93 <= y_p <= 98 or 93 <= n_p <= 98):
                    side, price = ("yes", y_p) if 93 <= y_p <= 98 else ("no", n_p)

                    # RSI filter — only addition from v5.2.5
                    if side == "yes" and current_rsi < RSI_CRASH_LIMIT:
                        log(f"⏭️ Skipping YES: RSI={current_rsi} below crash limit {RSI_CRASH_LIMIT}.")
                    elif side == "no" and current_rsi > RSI_SURGE_LIMIT:
                        log(f"⏭️ Skipping NO: RSI={current_rsi} above surge limit {RSI_SURGE_LIMIT}.")
                    else:
                        qty = int(min(MAX_POSITION_DOLLARS, (cash * risk_decimal)) * 100 // price)
                        if qty >= 1:
                            log(f"⚡ Pursuit: {side.upper()} @ {price}c (Qty: {qty}) | RSI: {current_rsi}")
                            success, actual_paid, filled_qty = place_order(market.ticker, side, qty, "buy", price)
                            if success and filled_qty > 0:
                                state["current_trade"] = {
                                    "ticker": market.ticker, "side": side, "count": filled_qty,
                                    "entry_price_cents": actual_paid, "actual_entry_price": actual_paid, "status": "filled"
                                }
                                save_state(state)
                                play_sound("buy")
                                log(f"✅ Filled: {filled_qty} contracts @ {actual_paid}c")
                                time.sleep(5)
                            else:
                                log("⚠️ Entry failed or zero fill. 15s Cooldown...")
                                time.sleep(15)

            time.sleep(1)
        except Exception as e:
            log(f"⚠️ Loop Error: {e}")
            time.sleep(5)