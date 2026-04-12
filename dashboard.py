import os
import json
import re
import threading
import pandas as pd
import pytz
from flask import Flask, render_template_string
from datetime import datetime

# --- CONFIGURATION ---
USER_PROFILE = os.environ['USERPROFILE']
FOLDER_NAME = 'mystic-bot'
FILE_PATH    = os.path.join(USER_PROFILE, 'Desktop', FOLDER_NAME, 'trades.json')
NGROK_KEY    = os.path.join(USER_PROFILE, 'Desktop', 'ngrok.txt')
LOG_FILE     = os.path.join(USER_PROFILE, 'Desktop', FOLDER_NAME, 'log.txt')

def start_ngrok():
    """Read authtoken from ngrok.txt and open a public tunnel on port 5000."""
    try:
        from pyngrok import ngrok, conf
        with open(NGROK_KEY, 'r') as f:
            token = f.read().strip()
        conf.get_default().auth_token = token
        tunnel = ngrok.connect(5000, "http")
        print(f"\n🌐 Public URL: {tunnel.public_url}\n")
    except FileNotFoundError:
        print("⚠️  ngrok.txt not found on Desktop — running local only.")
    except Exception as e:
        print(f"⚠️  ngrok failed: {e} — running local only.")

app = Flask(__name__)

# --- SCHEDULE DATA (matches bot v5.4.0 — auto risk scaling) ---
# Risk shown is for current balance tier. Tiers:
#   <$300 Recovery: 25% all windows
#   $300-$600 Building: 15% high, 12% mid/weekend
#   $600-$1500 Growth: 15% high, 10% overnight/mid, 8% weekend
#   $1500-$5000 Established: 12% high, 8% overnight/mid, 6% weekend
#   $5000+ Mature: 10% high, 5% overnight, 7% mid, 5% weekend

# Mirrors get_balance_tier() in bot v5.4.0+
BALANCE_TIERS = [
    {"max": 300,   "label": "Recovery (<$300)",    "overnight": "25%", "high": "25%", "mid": "25%", "weekend": "25%"},
    {"max": 600,   "label": "Building (<$600)",    "overnight": "15%", "high": "15%", "mid": "12%", "weekend": "12%"},
    {"max": 1500,  "label": "Growth (<$1,500)",    "overnight": "10%", "high": "15%", "mid": "10%", "weekend": "8%"},
    {"max": 5000,  "label": "Established (<$5k)",  "overnight": "8%",  "high": "12%", "mid": "8%",  "weekend": "6%"},
    {"max": 99999, "label": "Mature ($5k+)",        "overnight": "5%",  "high": "10%", "mid": "7%",  "weekend": "5%"},
]

def get_tier_for_balance(cash):
    for t in BALANCE_TIERS:
        if cash < t["max"]:
            return t
    return BALANCE_TIERS[-1]

STRATEGY_SCHEDULE = [
    {"days": "Mon-Fri", "range": range(0, 5), "start":    0, "end":  500, "time_str": "12:00am–5:00am", "risk_key": "overnight", "label": "Overnight"},
    {"days": "Mon-Fri", "range": range(0, 5), "start":  500, "end":  850, "time_str": "5:00am–8:30am",  "risk_key": "skip",      "label": "Pre-Market (Skipped)"},
    {"days": "Mon-Fri", "range": range(0, 5), "start": 1030, "end": 1200, "time_str": "10:30am–12:00pm","risk_key": "high",      "label": "High Confidence"},
    {"days": "Mon-Fri", "range": range(0, 5), "start": 1200, "end": 1600, "time_str": "12:00pm–4:00pm", "risk_key": "mid",       "label": "Balanced Midday"},
    {"days": "Mon-Fri", "range": range(0, 5), "start": 1630, "end": 1730, "time_str": "4:30pm–5:30pm",  "risk_key": "high",      "label": "Primary Window"},
    {"days": "All",     "range": range(0, 7), "start": 2200, "end": 2400, "time_str": "10:00pm–12:00am","risk_key": "overnight", "label": "Asian Open (7-day)"},
    {"days": "Saturday","range": [5],         "start":    0, "end": 1000, "time_str": "Sat 12am–10am",  "risk_key": "overnight", "label": "Saturday Overnight"},
    {"days": "Saturday","range": [5],         "start": 1000, "end": 1700, "time_str": "Sat 10am–5pm",   "risk_key": "weekend",   "label": "Saturday"},
    {"days": "Sunday",  "range": [6],         "start":    0, "end": 1700, "time_str": "Sun 12am–5pm",   "risk_key": "weekend",   "label": "Sunday"},
]

def get_current_window():
    tz_et = pytz.timezone('US/Eastern')
    now_et = datetime.now(tz_et)
    day = now_et.weekday()
    time_int = int(now_et.strftime('%H%M'))
    for window in STRATEGY_SCHEDULE:
        if day in window.get("range", []) and window["start"] <= time_int < window["end"]:
            return window
    return {"label": "Auto-Pilot (Passive)", "risk_key": "mid"}

def clean_val(value):
    if value is None or value == "": return 0.0
    is_neg = '-' in str(value)
    c = "".join(re.findall(r'[\d.]+', str(value)))
    try:
        v = float(c) if c else 0.0
        return -v if is_neg else v
    except: return 0.0

def get_log_lines(n=80):
    """
    Read last n lines of log.txt, parse and classify each line,
    collapse consecutive 500 API errors into a single summary row.
    Returns list of dicts: {time, icon, label, cls, raw}
    """
    if not os.path.exists(LOG_FILE):
        return []
    try:
        with open(LOG_FILE, 'r', encoding='utf-8', errors='replace') as f:
            lines = f.readlines()
    except Exception:
        return []

    lines = [l.rstrip() for l in lines if l.strip()][-n:]

    def classify(line):
        if '🏁 RESULT' in line:
            won = 'WIN' in line
            pnl_m = re.search(r'PnL: \$([+-]?\d+\.\d+)', line)
            pnl   = pnl_m.group(1) if pnl_m else ''
            ticker_m = re.search(r'RESULT: (\w+)', line)
            result = ticker_m.group(1) if ticker_m else ''
            label = f"Settlement — {result} — PnL: ${pnl}" if pnl else f"Settlement — {result}"
            return ('✅' if won else '❌', label, 'log-win' if won else 'log-loss')
        if '🚨 STOP LOSS' in line:
            price_m = re.search(r'Live: (\d+)c', line)
            sl_m    = re.search(r'SL: ([\d.]+)c', line)
            label = f"Stop-loss fired — live {price_m.group(1)}c vs SL {sl_m.group(1)}c" if price_m and sl_m else "Stop-loss fired"
            return ('🛑', label, 'log-stop')
        if '💸 Stop-loss complete' in line:
            pnl_m = re.search(r'PnL: \$([+-]?\d+\.\d+)', line)
            label = f"Stop-loss complete — PnL: ${pnl_m.group(1)}" if pnl_m else "Stop-loss complete"
            return ('💸', label, 'log-stop')
        if '⚡ Pursuit' in line:
            m = re.search(r'Pursuit: (\w+) @ (\d+)c \(Qty: (\d+)\)', line)
            label = f"Entering {m.group(1)} — {m.group(3)} contracts @ {m.group(2)}c" if m else "Entry attempt"
            return ('⚡', label, 'log-entry')
        if '✅ Filled' in line:
            m = re.search(r'Filled: (\d+) contracts @ (\d+)c', line)
            label = f"Filled {m.group(1)} contracts @ {m.group(2)}c" if m else "Order filled"
            return ('✅', label, 'log-fill')
        if '⏳ Finalizing' in line:
            m = re.search(r'Finalizing (KXBTC\S+)', line)
            label = f"Awaiting settlement — {m.group(1)}" if m else "Awaiting settlement"
            return ('⏳', label, 'log-info')
        if '⏭️ Skipping' in line or 'Skipping' in line:
            m = re.search(r'Skipping (.+?)\.?$', line)
            label = f"Skipped — {m.group(1)}" if m else "Entry skipped"
            return ('⏭️', label, 'log-skip')
        if '⏸️' in line:
            return ('⏸️', "Post stop-loss cooldown (60s)", 'log-skip')
        if '🪄 Magick Bot' in line:
            m = re.search(r'(Magick Bot .+)', line)
            return ('🤖', m.group(1) if m else "Bot started", 'log-info')
        if '⚠️ Loop Error' in line and '500' in line:
            return ('⚠️', "Kalshi API error (500)", 'log-error')
        if '⚠️ Entry failed' in line:
            return ('⚠️', "Entry failed — 15s cooldown", 'log-error')
        if '⚠️' in line or '❌' in line:
            clean = re.sub(r'\[.*? ET\]\s*', '', line)
            clean = re.sub(r'HTTP response.*', '', clean).strip()
            return ('⚠️', clean[:120], 'log-error')
        return None

    parsed = []
    i = 0
    while i < len(lines):
        line = lines[i]
        ts_m = re.match(r'\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) ET\]', line)
        ts = ts_m.group(1)[11:16] if ts_m else ''  # HH:MM only

        # Collapse consecutive 500 errors
        if '⚠️ Loop Error' in line and '500' in line:
            count = 1
            while i + count < len(lines) and '⚠️ Loop Error' in lines[i + count] and '500' in lines[i + count]:
                count += 1
            if count > 1:
                parsed.append({'time': ts, 'icon': '⚠️', 'label': f"Kalshi API outage — {count} errors suppressed", 'cls': 'log-error'})
                i += count
                continue

        result = classify(line)
        if result:
            icon, label, cls = result
            parsed.append({'time': ts, 'icon': icon, 'label': label, 'cls': cls})
        i += 1

    return list(reversed(parsed))  # newest first

def get_financial_data():
    if not os.path.exists(FILE_PATH):
        return {"error": f"File not found at: {FILE_PATH}"}
    try:
        with open(FILE_PATH, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except:
        return {"error": "Invalid JSON in trades.json"}

    df = pd.DataFrame(data)
    if df.empty:
        return {"error": "No trade data found."}

    t_col = 'timestamp' if 'timestamp' in df.columns else 'time'
    df['dt'] = pd.to_datetime(df[t_col], errors='coerce')
    df = df.dropna(subset=['dt']).sort_values('dt', ascending=False)
    df['trade_pnl'] = df['pnl'].apply(clean_val)
    now = datetime.now()

    total_trades = len(df)
    wins         = len(df[df['trade_pnl'] > 0])
    win_rate     = (wins / total_trades * 100) if total_trades > 0 else 0

    return {
        'df':           df,
        'total_pnl':    df['trade_pnl'].sum(),
        'daily_pnl':    df[df['dt'].dt.date == now.date()]['trade_pnl'].sum(),
        'win_rate':     win_rate,
        'total_trades': total_trades,
        'wins':         wins,
    }

@app.route('/')
def index():
    data        = get_financial_data()
    current_win = get_current_window()
    log_entries = get_log_lines(80)
    # Hardcoded to Growth tier
    current_tier = get_tier_for_balance(1000)

    if "error" in data:
        return f"<body style='background:#0d1117;color:white;padding:50px;'><h2>⚠️ Data Error</h2><p>{data['error']}</p></body>"

    # Cap at 50 most recent trades for the table
    trades_list = [
        {
            'time':     r['dt'].strftime('%m/%d %H:%M'),
            'pnl':      r['trade_pnl'],
            'result':   'WIN' if r['trade_pnl'] > 0 else 'LOSS',
            'category': str(r.get('category', 'bot')).lower() if 'category' in data['df'].columns else 'bot'
        }
        for _, r in data['df'].head(50).iterrows()
    ]

    html_template = """
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
        <style>
            :root { --bg: #0d1117; --panel: #161b22; --border: #30363d; --text: #c9d1d9; --blue: #58a6ff; --green: #3fb950; --red: #f85149; --gold: #d29922; }
            body { background: var(--bg); color: var(--text); font-family: sans-serif; padding: 15px; margin: 0; }

            .header { text-align: center; margin-bottom: 20px; }
            .header h1 { font-size: 26px; margin: 0; color: #fff; letter-spacing: 2px; text-transform: uppercase; }
            .status { color: var(--green); font-size: 11px; font-weight: bold; text-transform: uppercase; margin-top: 5px; }

            .active-banner { background: var(--panel); border: 1px solid var(--border); border-radius: 10px; padding: 15px; margin-bottom: 15px; display: flex; flex-wrap: wrap; justify-content: space-around; align-items: center; gap: 10px; }
            .banner-label { font-size: 10px; color: #8b949e; text-transform: uppercase; }
            .banner-val { font-size: 18px; font-weight: bold; color: var(--blue); }

            .stats-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(120px, 1fr)); gap: 10px; margin-bottom: 20px; }
            .card { background: var(--panel); padding: 15px; border-radius: 10px; border: 1px solid var(--border); text-align: center; }
            .card-label { font-size: 10px; color: #8b949e; text-transform: uppercase; }
            .card-val { font-size: 20px; font-weight: bold; display: block; margin-top: 5px; }
            .card-sub { font-size: 10px; color: #8b949e; margin-top: 3px; }

            .main-layout { display: flex; flex-wrap: wrap; gap: 20px; }
            .column { flex: 1; min-width: 300px; display: flex; flex-direction: column; gap: 20px; }

            .section-title { font-size: 11px; color: #8b949e; text-transform: uppercase; margin-bottom: 10px; font-weight: bold; }
            .panel { background: var(--panel); border: 1px solid var(--border); border-radius: 10px; padding: 15px; }

            .row { display: flex; align-items: center; justify-content: space-between; padding: 10px 0; border-bottom: 1px solid #21262d; font-size: 12px; }
            .row:last-child { border-bottom: none; }
            .current-row { background: rgba(56, 139, 253, 0.1); border-left: 4px solid var(--blue); padding-left: 10px; border-radius: 4px; }

            .table-container { max-height: 500px; overflow-y: auto; }
            table { width: 100%; border-collapse: collapse; font-size: 12px; }
            th { text-align: center; color: #8b949e; padding-bottom: 10px; font-weight: normal; }
            td { padding: 10px; text-align: center; border-bottom: 1px solid #21262d; }

            .pos { color: var(--green); } .neg { color: var(--red); }
            @media (max-width: 768px) { .column { min-width: 100%; } }

            .badge-bot    { background: rgba(88,166,255,0.15); color: var(--blue);  border: 1px solid rgba(88,166,255,0.3);  border-radius: 4px; padding: 2px 7px; font-size: 10px; font-weight: bold; }
            .badge-manual { background: rgba(210,153,34,0.15); color: var(--gold);  border: 1px solid rgba(210,153,34,0.3);  border-radius: 4px; padding: 2px 7px; font-size: 10px; font-weight: bold; }

            @keyframes pulse-dot {
                0%, 100% { opacity: 1; }
                50%       { opacity: 0.35; }
            }
            .pulse-dot { display: inline-block; animation: pulse-dot 2s ease-in-out infinite; }

            @keyframes pulse-glow {
                0%, 100% { text-shadow: 0 0 4px rgba(63,185,80,0.4); opacity: 1; }
                50%       { text-shadow: 0 0 12px rgba(63,185,80,0.9); opacity: 0.75; }
            }
            .status { color: var(--green); font-size: 11px; font-weight: bold; text-transform: uppercase; margin-top: 5px; animation: pulse-glow 2s ease-in-out infinite; }

            .log-panel { background: var(--panel); border: 1px solid var(--border); border-radius: 10px; padding: 15px; margin-top: 20px; }
            .log-row { display: flex; align-items: baseline; gap: 10px; padding: 6px 0; border-bottom: 1px solid #21262d; font-size: 12px; }
            .log-row:last-child { border-bottom: none; }
            .log-time { color: #8b949e; min-width: 42px; font-size: 11px; }
            .log-icon { min-width: 20px; text-align: center; }
            .log-label { flex: 1; }
            .log-win   .log-label { color: var(--green); }
            .log-loss  .log-label { color: #8b949e; }
            .log-stop  .log-label { color: var(--red); }
            .log-entry .log-label { color: var(--blue); }
            .log-fill  .log-label { color: var(--green); }
            .log-skip  .log-label { color: #8b949e; font-style: italic; }
            .log-error .log-label { color: var(--red); opacity: 0.7; }
            .log-info  .log-label { color: #8b949e; }
        </style>
        <meta http-equiv="refresh" content="30">
    </head>
    <body>
        <div class="header">
            <h1>Mystic Trader</h1>
            <div class="status"><span class="pulse-dot">●</span> LIVE &amp; TRADING</div>
        </div>

        <div class="active-banner">
            <div><div class="banner-label">Active Block</div><div class="banner-val">{{ window.label }}</div></div>
            <div><div class="banner-label">Current Risk</div><div class="banner-val" style="color:var(--green)">{{ current_risk }}</div></div>
        </div>

        <div class="stats-grid">
            <div class="card">
                <span class="card-label">Total PNL</span>
                <span class="card-val {{ 'pos' if total_pnl >= 0 else 'neg' }}">${{ "%.2f"|format(total_pnl) }}</span>
            </div>
            <div class="card">
                <span class="card-label">Today</span>
                <span class="card-val {{ 'pos' if daily_pnl >= 0 else 'neg' }}">${{ "%.2f"|format(daily_pnl) }}</span>
            </div>
            <div class="card">
                <span class="card-label">Win Rate</span>
                <span class="card-val pos">{{ "%.1f"|format(win_rate) }}%</span>
                <div class="card-sub">{{ wins }}W / {{ total_trades - wins }}L ({{ total_trades }} total)</div>
            </div>

        </div>

        <div class="main-layout">
            <div class="column">
                <div class="section-title">Schedule (ET)</div>
                <div class="panel">
                    {% for s in schedule %}
                    <div class="row {% if s.label == window.label %}current-row{% endif %}">
                        <span style="color:var(--blue); font-weight:bold; min-width:130px;">{{ s.time_str }}</span>
                        <span style="color:{% if s.risk_key == 'skip' %}#8b949e{% else %}var(--green){% endif %}; font-weight:bold; min-width:35px;">{{ '5%' if s.risk_key == 'fixed_5' else (tier[s.risk_key] if s.risk_key != 'skip' else '—') }}</span>
                        <span style="flex:1; text-align:right;">{{ s.label }}</span>
                    </div>
                    {% endfor %}
                    <div class="row {% if window.label == 'Auto-Pilot (Passive)' %}current-row{% endif %}">
                        <span style="color:#8b949e; min-width:130px;">All other times</span>
                        <span style="color:#8b949e; min-width:35px;">—</span>
                        <span style="flex:1; text-align:right;">Standby (Skipped)</span>
                    </div>
                </div>
            </div>

            <div class="column">
                <div class="section-title">Recent Trades (last 50)</div>
                <div class="panel table-container">
                    <table>
                        <thead><tr><th>Time</th><th>PNL</th><th>Result</th><th>Type</th></tr></thead>
                        <tbody>
                            {% for row in trades %}
                            <tr>
                                <td>{{ row.time }}</td>
                                <td class="{{ 'pos' if row.pnl > 0 else 'neg' }}">${{ "%.2f"|format(row.pnl) }}</td>
                                <td class="{{ 'pos' if row.pnl > 0 else 'neg' }}" style="font-weight:bold;">{{ row.result }}</td>
                                <td>
                                    {% if row.category == 'manual' %}
                                    <span class="badge-manual">Manual</span>
                                    {% else %}
                                    <span class="badge-bot">Bot</span>
                                    {% endif %}
                                </td>
                            </tr>
                            {% endfor %}
                        </tbody>
                    </table>
                </div>
            </div>


        </div>

        <!-- LIVE LOG -->
        <div style="margin-top: 20px;">
            <div class="section-title">Live Log (last 80 events, newest first)</div>
            <div class="log-panel" style="max-height: 500px; overflow-y: auto;">
                {% for entry in log_entries %}
                <div class="log-row {{ entry.cls }}">
                    <span class="log-time">{{ entry.time }}</span>
                    <span class="log-icon">{{ entry.icon }}</span>
                    <span class="log-label">{{ entry.label }}</span>
                </div>
                {% else %}
                <div style="color:#8b949e; font-size:12px; text-align:center; padding:20px;">No log entries found.</div>
                {% endfor %}
            </div>
        </div>

    </body>
    </html>
    """
    return render_template_string(
        html_template,
        trades=trades_list,
        total_pnl=data['total_pnl'],
        daily_pnl=data['daily_pnl'],
        win_rate=data['win_rate'],
        total_trades=data['total_trades'],
        wins=data['wins'],
        window=current_win,
        schedule=STRATEGY_SCHEDULE,
        log_entries=log_entries,
        tier=current_tier,
        current_risk=current_tier.get(current_win.get('risk_key', 'mid'), '—'),
    )

if __name__ == '__main__':
    print("\n🚀 DASHBOARD READY\n🔗 http://localhost:5000")
    threading.Thread(target=start_ngrok, daemon=True).start()
    app.run(host='127.0.0.1', port=5000, debug=False, use_reloader=False)