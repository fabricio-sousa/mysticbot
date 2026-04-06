import os
import json
import re
import pandas as pd
import pytz
from flask import Flask, render_template_string
from pyngrok import ngrok, conf
from datetime import datetime

# --- CONFIGURATION ---
USER_PROFILE = os.environ['USERPROFILE']
FOLDER_NAME = 'mystic-bot'
FILE_PATH = os.path.join(USER_PROFILE, 'Desktop', FOLDER_NAME, 'trades.json')
NGROK_AUTH_TOKEN = "3BY7aKR0ov1mqA8YhNOC61B3aRB_5fBPgqz19fqpv9FwwNTxm"

app = Flask(__name__)

# --- SCHEDULE DATA (matches bot v5.2.8) ---
STRATEGY_SCHEDULE = [
    {"days": "Mon-Fri", "range": range(0, 5), "start":    0, "end":  500, "time_str": "12:00am–5:00am", "risk": "3%",  "label": "Overnight"},
    {"days": "Mon-Fri", "range": range(0, 5), "start":  500, "end":  850, "time_str": "5:00am–8:30am",  "risk": "3%",  "label": "Pre-Market"},
    {"days": "Mon-Fri", "range": range(0, 5), "start": 1030, "end": 1200, "time_str": "10:30am–12:00pm","risk": "15%", "label": "High Confidence"},
    {"days": "Mon-Fri", "range": range(0, 5), "start": 1200, "end": 1600, "time_str": "12:00pm–4:00pm", "risk": "10%", "label": "Balanced Midday"},
    {"days": "Mon-Fri", "range": range(0, 5), "start": 1630, "end": 1730, "time_str": "4:30pm–5:30pm",  "risk": "15%", "label": "Primary Window"},
    {"days": "Mon-Fri", "range": range(0, 5), "start": 2200, "end": 2400, "time_str": "10:00pm–12:00am","risk": "3%",  "label": "Asian Open"},
    {"days": "Saturday","range": [5],         "start": 1000, "end": 1700, "time_str": "Sat 10am–5pm",   "risk": "5%",  "label": "Saturday"},
    {"days": "Sunday",  "range": [6],         "start": 1200, "end": 1700, "time_str": "Sun 12pm–5pm",   "risk": "5%",  "label": "Sunday Afternoon"},
]

def get_current_window():
    tz_et = pytz.timezone('US/Eastern')
    now_et = datetime.now(tz_et)
    day = now_et.weekday()
    time_int = int(now_et.strftime('%H%M'))
    for window in STRATEGY_SCHEDULE:
        if day in window.get("range", []) and window["start"] <= time_int < window["end"]:
            return window
    return {"label": "Auto-Pilot (Passive)", "risk": "1%"}

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

    if "error" in data:
        return f"<body style='background:#0d1117;color:white;padding:50px;'><h2>⚠️ Data Error</h2><p>{data['error']}</p></body>"

    # Cap at 50 most recent trades for the table
    trades_list = [
        {
            'time':   r['dt'].strftime('%m/%d %H:%M'),
            'pnl':    r['trade_pnl'],
            'result': 'WIN' if r['trade_pnl'] > 0 else 'LOSS'
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
        </style>
        <meta http-equiv="refresh" content="30">
    </head>
    <body>
        <div class="header">
            <h1>Mystic Bot</h1>
            <div class="status">● LIVE & PROTECTED</div>
        </div>

        <div class="active-banner">
            <div><div class="banner-label">Active Block</div><div class="banner-val">{{ window.label }}</div></div>
            <div><div class="banner-label">Risk</div><div class="banner-val" style="color:{{ 'var(--green)' if window.risk != '1%' else 'var(--gold)' }}">{{ window.risk }}</div></div>
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
                        <span style="color:var(--green); font-weight:bold; min-width:35px;">{{ s.risk }}</span>
                        <span style="flex:1; text-align:right;">{{ s.label }}</span>
                    </div>
                    {% endfor %}
                    <div class="row {% if window.label == 'Auto-Pilot (Passive)' %}current-row{% endif %}">
                        <span style="color:#8b949e; min-width:130px;">All other times</span>
                        <span style="color:var(--gold); min-width:35px;">1%</span>
                        <span style="flex:1; text-align:right;">Auto-Pilot</span>
                    </div>
                </div>
            </div>

            <div class="column">
                <div class="section-title">Recent Trades (last 50)</div>
                <div class="panel table-container">
                    <table>
                        <thead><tr><th>Time</th><th>PNL</th><th>Result</th></tr></thead>
                        <tbody>
                            {% for row in trades %}
                            <tr>
                                <td>{{ row.time }}</td>
                                <td class="{{ 'pos' if row.pnl > 0 else 'neg' }}">${{ "%.2f"|format(row.pnl) }}</td>
                                <td class="{{ 'pos' if row.pnl > 0 else 'neg' }}" style="font-weight:bold;">{{ row.result }}</td>
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
        window=current_win,
        schedule=STRATEGY_SCHEDULE,
    )

if __name__ == '__main__':
    conf.get_default().auth_token = NGROK_AUTH_TOKEN
    try:
        tunnels = ngrok.get_tunnels()
        for t in tunnels: ngrok.disconnect(t.public_url)
        public_url = ngrok.connect(5000).public_url
        print(f"\n🚀 DASHBOARD READY\n🔗 {public_url}\n")
        app.run(port=5000, debug=False, use_reloader=False)
    except Exception as e:
        print(f"Error: {e}")