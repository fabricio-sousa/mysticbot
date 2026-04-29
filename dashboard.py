import os
import json
import re
import pandas as pd
import pytz
from flask import Flask, render_template_string
from datetime import datetime

# --- CONFIGURATION ---
USER_PROFILE = os.environ['USERPROFILE']
FOLDER_NAME  = 'mystic-bot'
FILE_PATH    = os.path.join(USER_PROFILE, 'Desktop', FOLDER_NAME, 'trades.json')
LOG_FILE     = os.path.join(USER_PROFILE, 'Desktop', FOLDER_NAME, 'log.txt')

app = Flask(__name__)

BALANCE_TIERS = [
    {"max": 300,   "label": "Recovery (<$300)",    "overnight": "25%", "high": "25%", "mid": "25%", "weekend": "25%"},
    {"max": 600,   "label": "Building (<$600)",    "overnight": "15%", "high": "15%", "mid": "12%", "weekend": "12%"},
    {"max": 1500,  "label": "Growth (<$1,500)",    "overnight": "10%", "high": "15%", "mid": "10%", "weekend": "8%"},
    {"max": 5000,  "label": "Established (<$5k)",  "overnight": "8%",  "high": "12%", "mid": "8%",  "weekend": "6%"},
    {"max": 99999, "label": "Mature ($5k+)",        "overnight": "5%",  "high": "10%", "mid": "7%",  "weekend": "5%"},
]

def get_tier_for_balance(cash):
    for t in BALANCE_TIERS:
        if cash < t["max"]: return t
    return BALANCE_TIERS[-1]

STRATEGY_SCHEDULE = [
    {"range": range(0,5), "start":    0, "end":  500, "time_str": "12:00am–5:00am",  "risk_key": "overnight", "label": "Overnight"},
    {"range": range(0,5), "start":  500, "end":  850, "time_str": "5:00am–8:30am",   "risk_key": "skip",      "label": "Pre-Market (Skipped)"},
    {"range": range(0,5), "start": 1030, "end": 1200, "time_str": "10:30am–12:00pm", "risk_key": "high",      "label": "High Confidence"},
    {"range": range(0,5), "start": 1200, "end": 1600, "time_str": "12:00pm–4:00pm",  "risk_key": "mid",       "label": "Balanced Midday"},
    {"range": range(0,5), "start": 1630, "end": 1730, "time_str": "4:30pm–5:30pm",   "risk_key": "high",      "label": "Primary Window"},
    {"range": range(0,5), "start": 1730, "end": 2200, "time_str": "5:30pm–10:00pm",  "risk_key": "skip",      "label": "Evening (Disabled)"},
    {"range": range(0,7), "start": 2200, "end": 2400, "time_str": "10:00pm–12:00am", "risk_key": "overnight", "label": "Asian Open"},
    {"range": [5],        "start":    0, "end":  500, "time_str": "Sat 12am–5am",    "risk_key": "overnight", "label": "Saturday Overnight"},
    {"range": [5],        "start":  500, "end":  850, "time_str": "Sat 5am–8:30am",  "risk_key": "skip",      "label": "Saturday Pre-Market (Skipped)"},
    {"range": [5],        "start":  850, "end": 1700, "time_str": "Sat 8:30am–5pm",  "risk_key": "weekend",   "label": "Saturday"},
    {"range": [6],        "start":    0, "end":  500, "time_str": "Sun 12am–5am",    "risk_key": "overnight", "label": "Sunday Overnight"},
    {"range": [6],        "start":  500, "end":  850, "time_str": "Sun 5am–8:30am",  "risk_key": "skip",      "label": "Sunday Pre-Market (Skipped)"},
    {"range": [6],        "start":  850, "end": 1700, "time_str": "Sun 8:30am–5pm",  "risk_key": "weekend",   "label": "Sunday"},
]

def get_current_window():
    tz_et  = pytz.timezone('US/Eastern')
    now_et = datetime.now(tz_et)
    day    = now_et.weekday()
    ti     = int(now_et.strftime('%H%M'))
    for w in STRATEGY_SCHEDULE:
        if day in w.get("range", []) and w["start"] <= ti < w["end"]:
            return w
    return {"label": "Standby (Skipped)", "risk_key": "skip"}

def clean_val(value):
    if value is None or value == "": return 0.0
    is_neg = '-' in str(value)
    c = "".join(re.findall(r'[\d.]+', str(value)))
    try:
        v = float(c) if c else 0.0
        return -v if is_neg else v
    except: return 0.0

def get_financial_data():
    if not os.path.exists(FILE_PATH):
        return {"error": f"File not found: {FILE_PATH}"}
    try:
        with open(FILE_PATH, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except:
        return {"error": "Invalid JSON in trades.json"}

    df = pd.DataFrame(data)
    if df.empty:
        return {"error": "No trade data found."}

    t_col = 'timestamp' if 'timestamp' in df.columns else 'time'
    df['dt']        = pd.to_datetime(df[t_col], errors='coerce')
    df              = df.dropna(subset=['dt']).sort_values('dt', ascending=False)
    df['trade_pnl'] = df['pnl'].apply(clean_val)
    today           = datetime.now().date()

    total_trades = len(df)
    wins         = len(df[df['trade_pnl'] > 0])
    losses       = len(df[df['trade_pnl'] < 0])
    win_rate     = (wins / total_trades * 100) if total_trades > 0 else 0
    daily_df     = df[df['dt'].dt.date == today]

    return {
        'df':           df,
        'total_pnl':    df['trade_pnl'].sum(),
        'daily_pnl':    daily_df['trade_pnl'].sum(),
        'win_rate':     win_rate,
        'total_trades': total_trades,
        'wins':         wins,
        'losses':       losses,
        'daily_trades': len(daily_df),
        'daily_wins':   len(daily_df[daily_df['trade_pnl'] > 0]),
    }

@app.route('/')
def index():
    data         = get_financial_data()
    current_win  = get_current_window()
    current_tier = get_tier_for_balance(3101)  # update as balance grows

    if "error" in data:
        return f"<body style='background:#0d1117;color:white;padding:50px;'><h2>⚠️ {data['error']}</h2></body>"

    trades_list = [
        {
            'time':   r['dt'].strftime('%m/%d %H:%M'),
            'pnl':    r['trade_pnl'],
            'result': 'WIN' if r['trade_pnl'] > 0 else 'LOSS',
            'type':   str(r.get('type', '')),
        }
        for _, r in data['df'].head(50).iterrows()
    ]

    html_template = """
<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width,initial-scale=1.0,maximum-scale=1.0,user-scalable=no">
    <meta http-equiv="refresh" content="30">
    <style>
        :root{--bg:#0d1117;--panel:#161b22;--border:#30363d;--text:#c9d1d9;--blue:#58a6ff;--green:#3fb950;--red:#f85149;}
        *{box-sizing:border-box;margin:0;padding:0}
        body{background:var(--bg);color:var(--text);font-family:sans-serif;padding:15px}

        .header{text-align:center;margin-bottom:18px}
        .header h1{font-size:26px;color:#fff;letter-spacing:2px;text-transform:uppercase}
        @keyframes pulse-dot{0%,100%{opacity:1}50%{opacity:.35}}
        @keyframes pulse-glow{0%,100%{text-shadow:0 0 4px rgba(63,185,80,.4);opacity:1}50%{text-shadow:0 0 12px rgba(63,185,80,.9);opacity:.75}}
        .pulse-dot{display:inline-block;animation:pulse-dot 2s ease-in-out infinite}
        .status{color:var(--green);font-size:11px;font-weight:bold;text-transform:uppercase;margin-top:5px;animation:pulse-glow 2s ease-in-out infinite}

        .active-banner{background:var(--panel);border:1px solid var(--border);border-radius:10px;padding:14px;margin-bottom:14px;display:flex;flex-wrap:wrap;justify-content:space-around;align-items:center;gap:10px}
        .banner-label{font-size:10px;color:#8b949e;text-transform:uppercase}
        .banner-val{font-size:18px;font-weight:bold;color:var(--blue)}

        .stats-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:10px;margin-bottom:18px}
        .card{background:var(--panel);padding:14px;border-radius:10px;border:1px solid var(--border);text-align:center}
        .card-label{font-size:10px;color:#8b949e;text-transform:uppercase}
        .card-val{font-size:20px;font-weight:bold;display:block;margin-top:5px}
        .card-sub{font-size:10px;color:#8b949e;margin-top:3px}

        .main-layout{display:grid;grid-template-columns:1fr 1fr;gap:18px}
        .section-title{font-size:11px;color:#8b949e;text-transform:uppercase;margin-bottom:8px;font-weight:bold}
        .panel{background:var(--panel);border:1px solid var(--border);border-radius:10px;padding:14px}

        .row{display:flex;align-items:center;justify-content:space-between;padding:9px 0;border-bottom:1px solid #21262d;font-size:12px}
        .row:last-child{border:none}
        .current-row{background:rgba(56,139,253,.1);border-left:4px solid var(--blue);padding-left:8px;border-radius:4px}

        .table-container{max-height:500px;overflow-y:auto}
        table{width:100%;border-collapse:collapse;font-size:12px}
        th{text-align:center;color:#8b949e;padding-bottom:8px;font-weight:normal}
        td{padding:9px 5px;text-align:center;border-bottom:1px solid #21262d}
        tr:hover td{background:#1c2128}

        .pos{color:var(--green)}.neg{color:var(--red)}
        .badge-stop{background:#3a0808;color:var(--red);font-size:9px;padding:1px 5px;border-radius:3px;font-weight:bold}
        @media(max-width:768px){.main-layout{grid-template-columns:1fr}}
    </style>
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
            <span class="card-label">Session PNL</span>
            <span class="card-val {{ 'pos' if total_pnl >= 0 else 'neg' }}">${{ "%.2f"|format(total_pnl) }}</span>
        </div>
        <div class="card">
            <span class="card-label">Today</span>
            <span class="card-val {{ 'pos' if daily_pnl >= 0 else 'neg' }}">${{ "%.2f"|format(daily_pnl) }}</span>
            <div class="card-sub">{{ daily_wins }}W of {{ daily_trades }} trades</div>
        </div>
        <div class="card">
            <span class="card-label">Win Rate</span>
            <span class="card-val pos">{{ "%.1f"|format(win_rate) }}%</span>
            <div class="card-sub">{{ wins }}W / {{ losses }}L ({{ total_trades }} total)</div>
        </div>
    </div>

    <div class="main-layout">
        <div>
            <div class="section-title">Schedule (ET)</div>
            <div class="panel">
                {% for s in schedule %}
                <div class="row {% if s.label == window.label %}current-row{% endif %}">
                    <span style="color:var(--blue);font-weight:bold;min-width:130px">{{ s.time_str }}</span>
                    <span style="color:{% if s.risk_key == 'skip' %}#8b949e{% else %}var(--green){% endif %};font-weight:bold;min-width:35px">
                        {{ tier[s.risk_key] if s.risk_key != 'skip' else '—' }}
                    </span>
                    <span style="flex:1;text-align:right;color:{% if s.risk_key == 'skip' %}#8b949e{% else %}var(--text){% endif %}">{{ s.label }}</span>
                </div>
                {% endfor %}
                <div class="row {% if window.label == 'Standby (Skipped)' %}current-row{% endif %}">
                    <span style="color:#8b949e;min-width:130px">All other times</span>
                    <span style="color:#8b949e;min-width:35px">—</span>
                    <span style="flex:1;text-align:right;color:#8b949e">Standby (Skipped)</span>
                </div>
            </div>
        </div>

        <div>
            <div class="section-title">Recent Trades (last 50)</div>
            <div class="panel table-container">
                <table>
                    <thead><tr><th>Time</th><th>PNL</th><th>Result</th></tr></thead>
                    <tbody>
                        {% for row in trades %}
                        <tr>
                            <td>{{ row.time }}</td>
                            <td class="{{ 'pos' if row.pnl > 0 else 'neg' }}">${{ "%.2f"|format(row.pnl) }}</td>
                            <td>
                                {% if 'STOP' in row.type %}
                                    <span class="badge-stop">STOP</span>
                                {% else %}
                                    <span class="{{ 'pos' if row.pnl > 0 else 'neg' }}" style="font-weight:bold">{{ row.result }}</span>
                                {% endif %}
                            </td>
                        </tr>
                        {% endfor %}
                    </tbody>
                </table>
            </div>
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
        losses=data['losses'],
        daily_trades=data['daily_trades'],
        daily_wins=data['daily_wins'],
        window=current_win,
        schedule=STRATEGY_SCHEDULE,
        tier=current_tier,
        current_risk='—' if current_win.get('risk_key') == 'skip'
                     else current_tier.get(current_win.get('risk_key', 'mid'), '—'),
    )

if __name__ == '__main__':
    print("\n🚀 DASHBOARD READY\n🔗 http://localhost:5000")
    app.run(host='127.0.0.1', port=5000, debug=False, use_reloader=False)