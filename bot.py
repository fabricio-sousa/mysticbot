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
BASE_DIR     = os.path.dirname(os.path.abspath(__file__))
APIKEY_FILE  = os.path.join(BASE_DIR, "apikey.txt")
PRIVATE_FILE = os.path.join(BASE_DIR, "private.txt")
STATE_FILE   = os.path.join(BASE_DIR, "state.json")
TRADES_FILE  = os.path.join(BASE_DIR, "trades.json")
LOG_FILE     = os.path.join(BASE_DIR, "log.txt")

MAX_SLIPPAGE         = 2
MAX_POSITION_DOLLARS = 500.0
SAFETY_FLOOR         = 1000.0
STRIKE_LIMIT         = 3
STOP_LOSS_THRESHOLD  = 0.40

# --- RSI ---
RSI_PERIOD      = 9
RSI_CRASH_LIMIT = 30   # Skip YES entry if RSI too low (bearish momentum)
RSI_SURGE_LIMIT = 70   # Skip NO  entry if RSI too high (bullish momentum)

# ====================== HELPERS ======================
def log(msg: str):
    ts = datetime.now(pytz.timezone("US/Eastern")).strftime("%Y-%m-%d %H:%M:%S ET")
    print(f"\n[{ts}] {msg}")
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(f"[{ts}] {msg}\n")

def load_state() -> dict:
    """Load persisted state, merging with defaults so new keys are always present."""
    default = {
        "strikes":          0,
        "current_trade":    None,
        "pending_order":    False,
        "pending_order_id": None,
        "session_pnl":      0.0,
    }
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            try:
                return {**default, **json.load(f)}
            except Exception:
                pass
    return default

def save_state(state: dict):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)

def update_trades_json(trade_entry: dict):
    trades = []
    trade_entry["category"] = "bot"
    try:
        if os.path.exists(TRADES_FILE):
            with open(TRADES_FILE, "r") as f:
                try:
                    trades = json.load(f)
                except Exception:
                    trades = []
        trades.append(trade_entry)
        with open(TRADES_FILE, "w") as f:
            json.dump(trades, f, indent=2)
    except Exception as e:
        log(f"⚠️ JSON Error: {e}")

def safe_price_cents(value) -> int:
    try:
        return int(round(float(value or 0) * 100))
    except Exception:
        return 0

def play_sound(event_type: str):
    if not HAS_WINDOWS:
        return
    sounds = {
        "buy":         [(2000, 200)],
        "settle_win":  [(2500, 200), (3000, 200)],
        "settle_loss": [(600, 500)],
        "stop":        [(400, 1000)],
    }
    for freq, dur in sounds.get(event_type, []):
        winsound.Beep(freq, dur)

# ====================== DYNAMIC RISK ENGINE ======================
def get_dynamic_risk() -> tuple[float, bool]:
    tz  = pytz.timezone("US/Eastern")
    now = datetime.now(tz)
    day = now.weekday()
    t   = now.hour + now.minute / 60.0
    if 0 <= day <= 4:
        if 10.5 <= t < 12.0: return 0.15, True
        if 12.0 <= t < 16.0: return 0.10, True
        if 16.5 <= t < 17.5: return 0.15, True
    elif day == 6:
        if 12.0 <= t < 17.0: return 0.05, True
    return 0.01, False

# ====================== TECHNICAL ANALYTICS ======================
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

# ====================== ORDER ENGINE ======================
def place_order(client, ticker: str, side: str, count: int,
                action: str, price_cents: int) -> tuple[bool, int, int]:
    """
    Submit a limit order and poll until filled or timeout.
    On timeout: runs a final fill check, then cancels the order on Kalshi
    to prevent ghost fills on future ticks.
    Returns (success, actual_price_cents, filled_qty).
    """
    ex_id = None
    try:
        order_id = str(uuid.uuid4())
        limit    = min(99, price_cents + MAX_SLIPPAGE) if action == "buy" \
                   else max(1, price_cents - MAX_SLIPPAGE)

        resp  = client.create_order(
            ticker=ticker, side=side, action=action, count=count,
            type="limit", client_order_id=order_id,
            yes_price=limit if side == "yes" else None,
            no_price =limit if side == "no"  else None,
        )
        ex_id = resp.order.order_id

        # 25 × 2s = 50s polling window
        for _ in range(25):
            time.sleep(2)
            order = client.get_order(ex_id).order
            qty   = getattr(order, "filled", 0)
            if qty > 0:
                avg = getattr(order, "avg_fill_price", 0)
                if avg == 0:
                    log("⚠️ avg_fill_price is 0 — using limit price as estimate. PnL may be slightly off.")
                return True, (avg if avg > 0 else price_cents), qty

        # Final check: catch fills that landed just after the loop
        log(f"⚠️ Order {ex_id} unfilled after polling window. Running final check...")
        try:
            final_order = client.get_order(ex_id).order
            qty = getattr(final_order, "filled", 0)
            if qty > 0:
                avg = getattr(final_order, "avg_fill_price", 0)
                if avg == 0:
                    log("⚠️ Late fill: avg_fill_price is 0 — using limit price as estimate.")
                log(f"⚠️ Late fill detected: {qty} contracts @ {avg if avg > 0 else price_cents}c")
                return True, (avg if avg > 0 else price_cents), qty
        except Exception as e:
            log(f"⚠️ Final order check failed: {e}")

        # Cancel the order so it can't fill on a future tick as a ghost
        log(f"🚫 Cancelling unfilled order {ex_id}...")
        try:
            client.cancel_order(ex_id)
            log(f"✅ Order {ex_id} cancelled successfully.")
        except Exception as ce:
            log(f"⚠️ Cancel failed for {ex_id}: {ce} — verify on Kalshi manually.")

        return False, 0, 0

    except Exception as e:
        log(f"❌ Order Error: {e}")
        # If we have an order ID but hit an unexpected exception, attempt cancel
        if ex_id:
            try:
                client.cancel_order(ex_id)
                log(f"🚫 Order {ex_id} cancelled after unexpected error.")
            except Exception:
                pass
        return False, 0, 0

# ====================== RECOVERY ENGINE ======================
def sync_portfolio_to_state(client, state: dict):
    """
    Adopt any open KXBTC position not tracked in state (e.g. after a crash).
    Uses pending_order_id to recover actual fill price where possible.
    """
    try:
        resp      = client.get_positions()
        positions = getattr(resp, "market_positions", getattr(resp, "positions", []))

        for p in positions:
            p_qty = getattr(p, "market_position", getattr(p, "position", 0))
            if "KXBTC" in p.ticker and p_qty != 0:
                if not state["current_trade"]:
                    side = "yes" if p_qty > 0 else "no"
                    qty  = abs(p_qty)

                    entry_price = 0
                    pending_id  = state.get("pending_order_id")
                    if pending_id:
                        try:
                            recovered_order = client.get_order(pending_id).order
                            avg = getattr(recovered_order, "avg_fill_price", 0)
                            if avg > 0:
                                entry_price = avg
                                log(f"🔄 Recovery: Adopting {qty}x {side.upper()} on {p.ticker} "
                                    f"@ {entry_price}c (price recovered from order {pending_id}).")
                            else:
                                log(f"🔄 Recovery: Adopting {qty}x {side.upper()} on {p.ticker}. "
                                    f"⚠️ Could not recover price from order — stop-loss disabled.")
                        except Exception as e:
                            log(f"🔄 Recovery: Adopting {qty}x {side.upper()} on {p.ticker}. "
                                f"⚠️ Order lookup failed ({e}) — stop-loss disabled.")
                    else:
                        log(f"🔄 Recovery: Adopting {qty}x {side.upper()} on {p.ticker}. "
                            f"⚠️ No order ID in state — stop-loss disabled.")

                    state["current_trade"] = {
                        "ticker":             p.ticker,
                        "side":               side,
                        "count":              qty,
                        "actual_entry_price": entry_price,
                    }
                    state["pending_order"]    = False
                    state["pending_order_id"] = None
                    save_state(state)
                break

    except Exception as e:
        log(f"⚠️ Recovery sync error: {e}")

# ====================== MAIN LOOP ======================
if __name__ == "__main__":
    log("🪄 Magick Bot v5.3.5 Active (Cancel Guard)")

    # --- API init ---
    with open(APIKEY_FILE,  "r", encoding="utf-8") as f: api_key_id      = f.read().strip()
    with open(PRIVATE_FILE, "r", encoding="utf-8") as f: private_key_pem = f.read()
    config                 = Configuration(host="https://api.elections.kalshi.com/trade-api/v2")
    config.api_key_id      = api_key_id
    config.private_key_pem = private_key_pem
    client                 = KalshiClient(config)

    override_triggered = False

    while True:
        try:
            # --- Keyboard shortcuts (Windows only) ---
            if HAS_WINDOWS and msvcrt.kbhit():
                key = msvcrt.getch()
                if key == b'\x1b':
                    log("🛑 ESC pressed — exiting.")
                    os._exit(0)
                elif key.lower() == b'c':
                    override_triggered = True

            state  = load_state()
            now_et = datetime.now(pytz.timezone("US/Eastern"))

            # --- Balance ---
            try:
                cash = client.get_balance().balance / 100.0
            except Exception as e:
                log(f"⚠️ Balance API error: {e}")
                time.sleep(2)
                continue

            # --- Manual override ---
            if override_triggered:
                log("🛠️ Manual Override: Clearing current_trade and pending_order.")
                state["current_trade"]    = None
                state["pending_order"]    = False
                state["pending_order_id"] = None
                save_state(state)
                override_triggered = False

            # --- Hard shutdown checks ---
            if cash <= SAFETY_FLOOR:
                log(f"🚨 Shutdown: Cash ${cash:.2f} below safety floor ${SAFETY_FLOOR:.2f}")
                break
            if state["strikes"] >= STRIKE_LIMIT:
                log(f"🚨 Shutdown: {state['strikes']} consecutive losses hit strike limit.")
                break

            # --- Portfolio recovery ---
            sync_portfolio_to_state(client, state)
            curr = state["current_trade"]

            # --- Market data ---
            try:
                resp    = client.get_markets(series_ticker="KXBTC15M", limit=5, status="open")
                markets = [
                    m for m in getattr(resp, "markets", [])
                    if (m.close_time - now_et).total_seconds() > 0
                ]
                markets.sort(key=lambda x: x.close_time)
                market    = markets[0] if markets else None
                time_left = (market.close_time - now_et).total_seconds() / 60.0 if market else 0
                y_ask     = safe_price_cents(market.yes_ask_dollars) if market else 0
                n_ask     = safe_price_cents(market.no_ask_dollars)  if market else 0
            except Exception as e:
                log(f"⚠️ Market fetch error: {e}")
                time.sleep(2)
                continue

            current_rsi             = get_btc_rsi()
            risk_decimal, is_window = get_dynamic_risk()

            # =================================================================
            # BLOCK 1: MONITORING — stop-loss + settlement
            # =================================================================
            if curr:
                try:
                    m_live = client.get_market(curr["ticker"]).market
                    status = getattr(m_live, "status", "").lower()

                    # --- Stop-loss ---
                    if status == "open":
                        live_bid = safe_price_cents(
                            m_live.yes_bid_dollars if curr["side"] == "yes"
                            else m_live.no_bid_dollars
                        )
                        entry_p = curr["actual_entry_price"]

                        if entry_p == 0:
                            pass  # Entry unknown — SL cannot fire safely
                        elif 0 < live_bid <= entry_p * (1 - STOP_LOSS_THRESHOLD):
                            log(f"🚨 STOP LOSS: {curr['ticker']} "
                                f"live={live_bid}c entry={entry_p}c "
                                f"threshold={entry_p * (1 - STOP_LOSS_THRESHOLD):.0f}c")
                            success, _, _ = place_order(
                                client, curr["ticker"], curr["side"],
                                curr["count"], "sell", live_bid
                            )
                            if success:
                                pnl = (live_bid - entry_p) * curr["count"] / 100.0
                                log(f"💸 Stop-loss filled. PnL: ${pnl:+.2f} | "
                                    f"Strikes now: {state['strikes'] + 1}")
                                update_trades_json({
                                    "timestamp": now_et.isoformat(),
                                    "ticker":    curr["ticker"],
                                    "side":      curr["side"],
                                    "pnl":       round(pnl, 2),
                                    "type":      "STOP_LOSS",
                                })
                                state["strikes"]          += 1
                                state["session_pnl"]      += pnl
                                state["current_trade"]     = None
                                state["pending_order"]     = False
                                state["pending_order_id"]  = None
                                save_state(state)
                                play_sound("stop")
                                continue

                    # --- Settlement retry loop ---
                    elif status in ["closed", "settled"]:
                        log(f"⏳ Waiting for result on {curr['ticker']}…")
                        settled = False

                        for attempt in range(30):
                            try:
                                m_check = client.get_market(curr["ticker"]).market
                                res     = getattr(m_check, "result", "").lower()
                                if res in ["yes", "no"]:
                                    won = (curr["side"] == res)
                                    ep  = curr["actual_entry_price"]
                                    pnl = (
                                        (100 - ep) * curr["count"] / 100.0 if won
                                        else -(ep * curr["count"] / 100.0)
                                    )
                                    log(f"🏁 SETTLED: {res.upper()} | "
                                        f"{'WIN' if won else 'LOSS'} | PnL: ${pnl:+.2f}")
                                    update_trades_json({
                                        "timestamp": now_et.isoformat(),
                                        "ticker":    curr["ticker"],
                                        "side":      curr["side"],
                                        "pnl":       round(pnl, 2),
                                        "type":      "SETTLEMENT",
                                    })
                                    state["strikes"]          = 0 if won else state["strikes"] + 1
                                    state["session_pnl"]     += pnl
                                    state["current_trade"]    = None
                                    state["pending_order"]    = False
                                    state["pending_order_id"] = None
                                    save_state(state)
                                    play_sound("settle_win" if won else "settle_loss")
                                    settled = True
                                    break

                            except Exception as e:
                                log(f"⚠️ Settlement poll error (attempt {attempt + 1}/30): {e}")

                            time.sleep(10)

                        if not settled:
                            log(f"⚠️ Settlement timeout on {curr['ticker']} after 5 min — "
                                f"manual review required.")

                except Exception as e:
                    log(f"⚠️ Monitor Error: {e}")

            # =================================================================
            # BLOCK 2: ENTRY LOGIC
            # =================================================================
            elif not state["pending_order"] and is_window and market:
                if 2.0 <= time_left <= 6.0:
                    if (93 <= y_ask <= 98) or (93 <= n_ask <= 98):
                        side  = "yes" if 93 <= y_ask <= 98 else "no"
                        price = y_ask if side == "yes" else n_ask

                        if side == "yes" and current_rsi < RSI_CRASH_LIMIT:
                            log(f"⏭️ Skipping YES: RSI={current_rsi} below crash limit {RSI_CRASH_LIMIT}.")
                        elif side == "no" and current_rsi > RSI_SURGE_LIMIT:
                            log(f"⏭️ Skipping NO: RSI={current_rsi} above surge limit {RSI_SURGE_LIMIT}.")
                        else:
                            qty = int(
                                min(MAX_POSITION_DOLLARS, cash * risk_decimal) * 100 // price
                            )
                            if qty >= 1:
                                pending_id                = str(uuid.uuid4())
                                state["pending_order"]    = True
                                state["pending_order_id"] = pending_id
                                save_state(state)
                                log(f"⚡ Entry: {side.upper()} @ {price}c x{qty} "
                                    f"| RSI={current_rsi} | {time_left:.1f}m left")
                                try:
                                    success, actual_paid, filled_qty = place_order(
                                        client, market.ticker, side, qty, "buy", price
                                    )
                                    if success and filled_qty > 0:
                                        state["current_trade"] = {
                                            "ticker":             market.ticker,
                                            "side":               side,
                                            "count":              filled_qty,
                                            "actual_entry_price": actual_paid,
                                        }
                                        save_state(state)
                                        play_sound("buy")
                                        log(f"✅ Filled: {filled_qty}x {side.upper()} "
                                            f"@ {actual_paid}c on {market.ticker}")
                                    else:
                                        log("❌ Entry order failed or unfilled.")
                                finally:
                                    state["pending_order"]    = False
                                    state["pending_order_id"] = None
                                    save_state(state)

                                time.sleep(10)

            # --- Heartbeat ---
            trade_tag = f" [IN: {curr['side'].upper()}]" if curr else ""
            print(
                f"\r[{now_et.strftime('%H:%M:%S')}] "
                f"RSI: {current_rsi} | Cash: ${cash:.2f} | "
                f"PnL: ${state['session_pnl']:+.2f} | "
                f"Strikes: {state['strikes']}{trade_tag}   ",
                end=""
            )

            time.sleep(1)

        except Exception as e:
            log(f"⚠️ Loop Error: {e}")
            try:
                s = load_state()
                if s["pending_order"]:
                    s["pending_order"] = False
                    save_state(s)
                    log("🔓 Pending lock released by exception handler.")
            except Exception:
                pass
            time.sleep(5)