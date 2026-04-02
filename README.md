# 🪄 Magick Bot v5.0.9

Magick Bot is an automated algorithmic trading system optimized for the **Kalshi Bitcoin 15-Minute (KXBTC15M)** markets. It is designed to execute high-probability trades (94%-98% confidence) within specific time-of-day risk parameters.

## 🚀 Core Logic & Strategy

The bot operates on a **Strategic Allocation Mode**, where risk is not static but fluctuates based on historically high-performing time windows.

* **Targeting:** It scans for `KXBTC15M` series markets that are currently open.
* **Entry Window:** Executes trades strictly between **6.0 and 1.5 minutes** before market settlement.
* **Probability Trigger:** Enters a position only when the **Bid** for YES or NO is between **94¢ and 98¢**.
* **Dynamic Sizing:** Calculates quantity based on a percentage of current cash, capped by a maximum dollar exposure.

## 🛡️ Security & Safety Features (The "Circuit Breakers")

To protect the bankroll, the bot includes several hard-coded safety mechanisms:

1. **Safety Floor ($600):** If the total cash balance hits **$600.00**, the bot performs an emergency shutdown to preserve remaining capital.
2. **Strike Limit (3):** Tracks consecutive losses. If the bot hits **3 losses in a row**, it shuts down to prevent "death spirals" during unpredictable market trends.
3. **Position Cap ($500):** Regardless of risk percentage, no single trade will exceed **$500.00** in total cost.
4. **Slippage Buffer:** Uses a **2¢ slippage allowance** (`MAX_SLIPPAGE`) to ensure fills in fast-moving 15-minute windows.
5. **Cooldown Timer:** Enforces a 5-second "breathe" period after every order exit before scanning for new opportunities.

## 📈 Trading Schedule (US Eastern Time)

The bot follows a strict ET-based risk profile:

| Day | Time Window (ET) | Risk Level |
| :--- | :--- | :--- |
| **Mon - Fri** | 02:00 AM – 05:00 AM | **15%** (High Conviction) |
| **Mon - Fri** | 05:00 AM – 08:30 AM | **5%** (Conservative) |
| **Mon - Fri** | 10:30 AM – 12:00 PM | **15%** (High Conviction) |
| **Mon - Fri** | 12:00 PM – 04:00 PM | **10%** (Standard) |
| **Mon - Fri** | 04:30 PM – 05:30 PM | **15%** (High Conviction) |
| **Mon - Fri** | 10:00 PM – 12:00 AM | **10%** (Evening Scalp) |
| **Sunday** | 12:00 PM – 05:00 PM | **5%** (Weekend Entry) |
| **Saturday** | All Day | **0%** (Market Dormant) |

## 🛠️ Setup & Local Environment

### 1. File Structure (Local Only)
The following files are **required** but are excluded from this repository via `.gitignore` for security:
* `apikey.txt`: Your Kalshi API ID.
* `private.txt`: Your Private PEM key.
* `state.json`: Tracks current strikes and active trade status.
* `trades.json`: Encrypted/Local history of all executed PnL.
* `log.txt`: Verbose system logs.

### 2. Installation
```bash
pip install kalshi-python-sync pytz