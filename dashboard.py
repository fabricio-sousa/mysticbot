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
FILE_PATH = os.path.join(USER_PROFILE, 'Desktop', 'mystic-bot', 'trades.json')
NGROK_AUTH_TOKEN = "3BY7aKR0ov1mqA8YhNOC61B3aRB_5fBPgqz19fqpv9FwwNTxm"

app = Flask(__name__)

# --- SCHEDULE DATA ---
STRATEGY_SCHEDULE = [
    {"days": "Mon-Fri", "range": range(0, 5), "start": 500, "end": 830, "time_str": "5:00am–8:30am", "risk": "5%", "label": "Safe / Low Priority"},
    {"days": "Mon-Fri", "range": range(0, 5), "start": 1030, "end": 1200, "time_str": "10:30am–12:00pm", "risk": "15%", "label": "High Confidence"},
    {"days": "Mon-Fri", "range": range(0, 5), "start": 1200, "end": 1600, "time_str": "12:00pm–4:00pm", "risk": "10%", "label": "Balanced Midday"},
    {"days": "Mon-Fri", "range": range(0, 5), "start": 1630, "end": 1730, "time_str": "4:30pm–5:30pm", "risk": "15%", "label": "Primary Window"},
    {"days": "Mon-Fri", "range": range(0, 5), "start": 2200, "end": 2400, "time_str": "10:00pm–12:00am", "risk": "10%", "label": "Asian Open"},
    {"days": "Mon-Fri", "range": range(0, 5), "start": 200, "end": 500, "time_str": "2:00am–5:00am", "risk": "15%", "label": "Safe Overnights"},
    {"days": "Sunday", "range": [6], "start": 1200, "end": 1700, "time_str": "Sun 12pm–5pm", "risk": "10%", "label": "Weekend Transitional"}
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
    if not os.path.exists(FILE_PATH): return None
    try:
        with open(FILE_PATH, 'r', encoding='utf-8') as f: data = json.load(f)
    except: return None
    df = pd.DataFrame(data)
    if df.empty: return None
    t_col = 'timestamp' if 'timestamp' in df.columns else 'time'
    df['dt'] = pd.to_datetime(df[t_col], errors='coerce')
    df = df.dropna(subset=['dt']).sort_values('dt', ascending=False)
    df['trade_pnl'] = df['pnl'].apply(clean_val)
    now = datetime.now()
    return {
        'df': df, 
        'total_pnl': df['trade_pnl'].sum(),
        'daily_pnl': df[df['dt'].dt.date == now.date()]['trade_pnl'].sum(),
        'win_rate': (len(df[df['trade_pnl'] > 0]) / len(df) * 100) if not df.empty else 0
    }

@app.route('/')
def index():
    data = get_financial_data()
    current_win = get_current_window()
    if not data: return "<h1>Waiting for data...</h1>"
    
    trades_list = [{'time': r['dt'].strftime('%m/%d %H:%M'), 'pnl': r['trade_pnl'], 'result': 'WIN' if r['trade_pnl'] > 0 else 'LOSS'} for _, r in data['df'].iterrows()]

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

            /* Top Info Banner */
            .active-banner { background: var(--panel); border: 1px solid var(--border); border-radius: 10px; padding: 15px; margin-bottom: 15px; display: flex; flex-wrap: wrap; justify-content: space-around; align-items: center; gap: 10px; }
            .banner-label { font-size: 10px; color: #8b949e; text-transform: uppercase; }
            .banner-val { font-size: 18px; font-weight: bold; color: var(--blue); }

            /* Main Stats Row */
            .stats-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(120px, 1fr)); gap: 10px; margin-bottom: 20px; }
            .card { background: var(--panel); padding: 15px; border-radius: 10px; border: 1px solid var(--border); text-align: center; }
            .card-val { font-size: 20px; font-weight: bold; display: block; margin-top: 5px; }

            /* Desktop Grid System (3 Columns) */
            .main-layout { display: flex; flex-wrap: wrap; gap: 20px; }
            .column { flex: 1; min-width: 300px; display: flex; flex-direction: column; gap: 20px; }
            
            .section-title { font-size: 11px; color: #8b949e; text-transform: uppercase; margin-bottom: 10px; font-weight: bold; letter-spacing: 1px; }
            .panel { background: var(--panel); border: 1px solid var(--border); border-radius: 10px; padding: 15px; }
            
            /* UI Rows */
            .row { display: flex; align-items: center; justify-content: space-between; padding: 10px 0; border-bottom: 1px solid #21262d; font-size: 12px; }
            .row:last-child { border-bottom: none; }
            .current-row { background: rgba(56, 139, 253, 0.1); border-left: 4px solid var(--blue); padding-left: 10px; }
            
            /* Table Styling */
            .table-container { max-height: 500px; overflow-y: auto; }
            table { width: 100%; border-collapse: collapse; font-size: 12px; }
            th { text-align: center; color: #8b949e; padding-bottom: 10px; font-weight: normal; }
            td { padding: 10px; text-align: center; }

            /* Color States */
            .pos { color: var(--green); } .neg { color: var(--red); }
            .security-tag { color: var(--red); font-weight: bold; }
            .passive-badge { background: var(--gold); color: #000; padding: 4px 12px; border-radius: 12px; font-size: 10px; font-weight: bold; }
            .live-badge { background: var(--green); color: #000; padding: 4px 12px; border-radius: 12px; font-size: 10px; font-weight: bold; }

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
            <div>
                {% if window.risk == "1%" %}<span class="passive-badge">PASSIVE</span>{% else %}<span class="live-badge">LIVE</span>{% endif %}
            </div>
        </div>

        <div class="stats-grid">
            <div class="card"><span class="banner-label">Total PNL</span><span class="card-val {{ 'pos' if total_pnl >= 0 else 'neg' }}">${{ "%.2f"|format(total_pnl) }}</span></div>
            <div class="card"><span class="banner-label">Daily</span><span class="card-val {{ 'pos' if daily_pnl >= 0 else 'neg' }}">${{ "%.2f"|format(daily_pnl) }}</span></div>
            <div class="card"><span class="banner-label">Win Rate</span><span class="card-val pos">{{ "%.1f"|format(win_rate) }}%</span></div>
        </div>

        <div class="main-layout">
            <div class="column">
                <div class="section-title">Strategy Schedule (ET)</div>
                <div class="panel">
                    {% for s in schedule %}
                    <div class="row {% if s.label == window.label %}current-row{% endif %}">
                        <span style="color:var(--blue); font-weight:bold; min-width:110px;">{{ s.time_str }}</span>
                        <span style="color:var(--green); font-weight:bold; width:40px;">{{ s.risk }}</span>
                        <span style="flex:1; text-align:right; color:#fff;">{{ s.label }}</span>
                    </div>
                    {% endfor %}
                    <div class="row {% if window.label == 'Auto-Pilot (Passive)' %}current-row{% endif %}">
                        <span style="color:#8b949e; min-width:110px;">Other Times</span>
                        <span style="color:var(--gold); width:40px;">1%</span>
                        <span style="flex:1; text-align:right;">Auto-Pilot</span>
                    </div>
                </div>
            </div>

            <div class="column">
                <div class="section-title">Recent Trades</div>
                <div class="panel table-container">
                    <table>
                        <thead><tr><th>Time</th><th>PNL</th><th>Result</th></tr></thead>
                        <tbody>
                            {% for row in trades %}
                            <tr style="border-bottom: 1px solid #21262d;">
                                <td>{{ row.time }}</td>
                                <td class="{{ 'pos' if row.pnl > 0 else 'neg' }}">${{ "%.2f"|format(row.pnl) }}</td>
                                <td class="{{ 'pos' if row.pnl > 0 else 'neg' }}" style="font-weight:bold;">{{ row.result }}</td>
                            </tr>
                            {% endfor %}
                        </tbody>
                    </table>
                </div>
            </div>

            <div class="column">
                <div class="section-title">Security Guardrails</div>
                <div class="panel">
                    <div class="row"><span>Core Engine</span><span style="color:var(--gold); font-weight:bold;">Scalp-Momentum</span></div>
                    <div class="row"><span>Stop Loss</span><span class="security-tag">40% Drawdown</span></div>
                    <div class="row"><span>Circuit Breaker</span><span class="security-tag">3-Strike Rule</span></div>
                    <div class="row"><span>Hard Floor</span><span class="security-tag">$1,000 Shutdown</span></div>
                    <div class="row"><span>Anti-Slippage</span><span style="color:var(--blue);">2¢ Max Deviation</span></div>
                    <div class="row"><span>Persistence</span><span style="color:#8b949e;">state.json Active</span></div>
                </div>
            </div>
        </div>
    </body>
    </html>
    """
    return render_template_string(html_template, 
                                trades=trades_list, 
                                total_pnl=data['total_pnl'], 
                                daily_pnl=data['daily_pnl'], 
                                win_rate=data['win_rate'], 
                                window=current_win, 
                                schedule=STRATEGY_SCHEDULE)

if __name__ == '__main__':
    conf.get_default().auth_token = NGROK_AUTH_TOKEN
    try:
        tunnels = ngrok.get_tunnels()
        for t in tunnels: ngrok.disconnect(t.public_url)
        public_url = ngrok.connect(5000).public_url
        print(f"\n🚀 DASHBOARD READY\n🔗 {public_url}\n")
        app.run(port=5000, debug=False, use_reloader=False)
    except Exception as e: print(f"Error: {e}")