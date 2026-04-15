# 🪄 Magick Bot (v5.5.0)

Magick Bot is a fully autonomous algorithmic trading system built for the **Kalshi Bitcoin 15-Minute (KXBTC15M)** prediction markets. It targets high-confidence binary contracts, manages risk dynamically based on account balance, and operates 24/7 across a structured weekly schedule.

---

## 🚀 Core Strategy

The bot scans for open `KXBTC15M` markets and enters positions only when a strict set of conditions are met simultaneously:

- **Entry window:** 2.0 to 6.0 minutes before contract settlement
- **Price trigger:** YES or NO bid between **93¢ and 98¢**
- **RSI filter:** 9-period RSI must be within the time-window-specific band
- **Volatility guard:** BTC 5-candle price range must be under **$300**
- **RSI recovery cooldown:** RSI must be stable for **4 consecutive ticks** after any extreme reading before entry is allowed

When all conditions are met, the bot calculates position size based on current balance and tier, places the order, and monitors it until settlement or stop-loss.

---

## 🛡️ Safety & Risk Controls

### Circuit Breakers
| Control | Value | Description |
|---|---|---|
| Safety Floor | **$1,200** | Emergency shutdown if balance drops below this |
| Strike Limit | **3** | Shuts down after 3 consecutive losses |
| Max Contracts | **150** | Hard cap on contracts per trade |
| Max Position | **$500** | Hard cap on dollar exposure per trade |
| Slippage Buffer | **2¢** | Allowance for fast-moving markets |
| Stop-Loss Threshold | **40%** | Exits trade if live bid drops 40% from entry price |
| Post-SL Cooldown | **60s** | Skips next entry window after a stop-loss fires |

### Stop-Loss Logic
The bot monitors every active trade in real time. If the live bid on the held side drops to ≤40% of the entry price, it immediately places a market sell order and records the loss. After a stop-loss, a 60-second cooldown prevents re-entry into the next window.

### Entry Lock
An in-memory `_entry_lock` flag prevents double-buy race conditions if the loop ticks multiple times during order placement.

### Skip Deduplication
Consecutive identical skip reasons (RSI, volatility, cooldown) are logged only once per category to keep logs clean and readable.

---

## 📈 Trading Schedule (US Eastern Time)

The bot follows a strict ET-based schedule. All windows auto-scale risk based on current balance tier.

| Window | Days | Risk |
|---|---|---|
| 12:00AM – 5:00AM | Mon–Fri | Overnight tier |
| 5:00AM – 8:30AM | Mon–Fri | **Skipped** (pre-market) |
| 10:30AM – 12:00PM | Mon–Fri | High tier |
| 12:00PM – 4:00PM | Mon–Fri | Mid tier |
| 4:30PM – 5:30PM | Mon–Fri | High tier |
| 5:30PM – 8:00PM | Mon–Fri | **5% fixed** (evening) |
| 8:00PM – 10:00PM | Mon–Fri | **Skipped** (buffer) |
| 10:00PM – 12:00AM | All 7 days | Overnight tier |
| 12:00AM – 10:00AM | Saturday | Overnight tier |
| 10:00AM – 5:00PM | Saturday | Weekend tier |
| 12:00AM – 5:00PM | Sunday | Weekend tier |
| All other times | Any | **Skipped** |

---

## ⚖️ Dynamic Risk Engine

Risk percentage is automatically determined by both the current time window and the current account balance. The bot re-evaluates this on every tick.

### Balance Tiers

| Balance | Mode | Overnight | High | Mid | Weekend |
|---|---|---|---|---|---|
| Under $300 | Recovery | 25% | 25% | 25% | 25% |
| $300 – $600 | Building | 15% | 15% | 12% | 12% |
| $600 – $1,500 | Growth | 10% | 15% | 10% | 8% |
| $1,500 – $5,000 | Established | 8% | 12% | 8% | 6% |
| $5,000+ | Mature | 5% | 10% | 7% | 5% |

Evening window (5:30–8PM) is always fixed at **5%** regardless of tier.

---

## 📊 RSI Filter — Time-Aware Bands

RSI bands are looser during low-volatility overnight/weekend sessions and tighter during high-activity US market hours.

| Window | RSI Low | RSI High |
|---|---|---|
| Overnight (12AM–5AM) | 25 | 75 |
| Asian Open (10PM–12AM) | 25 | 75 |
| Evening (5:30PM–8PM) | 30 | 70 |
| Weekend | 30 | 70 |
| US Market Hours (default) | 38 | 62 |

**RSI Recovery Cooldown:** After RSI exits an extreme zone, the bot requires 4 consecutive stable ticks before re-enabling entry. This prevents "dead cat bounce" entries immediately after volatile moves.

---

## 🔑 Keyboard Controls

| Key | Action |
|---|---|
| `ESC` | Graceful shutdown |
| `C` | Clear current trade state (does not sell on Kalshi) |

---

## 📊 Live Dashboard (`dashboard.py`)

A Flask-based web dashboard provides real-time visibility into bot performance.

**Features:**
- Session PnL, total PnL, daily PnL, win rate, trade count
- Current active window with risk percentage
- Full weekly schedule with live highlighting of current window
- Recent trades table (last 50) showing time, PnL, and result
- Remote access via **pyngrok** — generates a public URL for monitoring from any device

**Run the dashboard:**
```bash
python dashboard.py
```
Then open `http://localhost:5000` or use the ngrok URL printed at startup.

---

## 📂 File Structure

```
mystic-bot/
├── magick_bot.py       # Main bot
├── dashboard.py        # Web dashboard
├── apikey.txt          # Kalshi API Key ID (not in repo)
├── private.txt         # Kalshi RSA Private Key (not in repo)
├── state.json          # Active trade state and strike count (auto-generated)
├── trades.json         # Trade history and PnL log (auto-generated)
├── log.txt             # Verbose system log (auto-generated)
└── ngrok.txt           # ngrok auth token for dashboard remote access
```

---

## 🛠️ Setup

### Requirements
```bash
pip install kalshi-python-sync pytz flask pyngrok requests
```

### API Keys
1. Create `apikey.txt` — paste your Kalshi API Key ID
2. Create `private.txt` — paste your Kalshi RSA Private Key (PEM format)
3. Create `ngrok.txt` — paste your ngrok auth token (for dashboard remote access)

### Running the Bot
```bash
python magick_bot.py
```

### Running the Dashboard (separate terminal)
```bash
python dashboard.py
```

---

## 🖥️ Environment Notes

- **Windows required** for audio alerts (`winsound`) and keyboard interrupt detection (`msvcrt`)
- Bot will run on non-Windows systems but without sound and keyboard controls
- **Disable system sleep** while running overnight sessions — the bot stops trading if the computer sleeps. Set Windows Power Settings to "Never sleep" while plugged in.

---

## 📈 Observed Performance

Based on live trading from April 12–15, 2026:

| Session | Trades | Win Rate | Net PnL |
|---|---|---|---|
| Apr 12 full day | 40 | 100% | +$205.81 |
| Apr 12–13 overnight | 20 | 100% | +$123.96 |
| Apr 13 full day | 16 | 94% | +$29.48 |
| Apr 13–14 overnight | 16 | 100% | +$106.61 |
| Apr 14 full day (bot only) | 23 | 96% | +$72.91 |
| Apr 14–15 overnight | 19 | 100% | +$103.04 |

**Overall: 134W / 3L across tracked sessions**

---

## ⚠️ Disclaimer

This bot trades real money on prediction markets. Past performance does not guarantee future results. Geopolitical events, macro data releases, and sudden BTC volatility can cause stop-losses. Always monitor the morning macro check before running the bot during high-risk news periods.