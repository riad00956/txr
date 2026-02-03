import os
import telebot
import sqlite3
import requests
import random
import string
import threading
import time
from datetime import datetime
from telebot import types
from apscheduler.schedulers.background import BackgroundScheduler
from http.server import HTTPServer, BaseHTTPRequestHandler

# ==============================
# CONFIGURATION
# ==============================
# Replace with your actual Token and Admin ID
API_TOKEN = '8225162929:AAExD7IKh-jpAXwPCQkLDP6wKgnJhUoKVJ0'
ADMIN_ID = 7832264582

bot = telebot.TeleBot(API_TOKEN)
scheduler = BackgroundScheduler(timezone="UTC")
scheduler.start()

# ==============================
# DATABASE SETUP
# ==============================
def init_db():
    conn = sqlite3.connect('uptime.db', check_same_thread=False)
    cursor = conn.cursor()
    cursor.execute('''CREATE TABLE IF NOT EXISTS monitors 
                      (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, url TEXT, 
                       interval INTEGER, status TEXT DEFAULT 'UNKNOWN', fail_count INTEGER DEFAULT 0)''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, is_verified INTEGER DEFAULT 0)''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS access_codes (code TEXT PRIMARY KEY, is_used INTEGER DEFAULT 0)''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS logs (id INTEGER PRIMARY KEY AUTOINCREMENT, monitor_id INTEGER, status TEXT, detail TEXT, timestamp TEXT)''')
    conn.commit()
    return conn

db_conn = init_db()

# ==============================
# MONITORING ENGINE
# ==============================
def ping_url(monitor_id, url, user_id):
    regions = ["🇺🇸 US-East", "🇪🇺 EU-West", "🇸🇬 SG-Core", "🇯🇵 JP-Tokyo"]
    region = random.choice(regions)
    headers = {'User-Agent': 'UptimeBot/2.0 (Health-Check)'}
    
    try:
        start_time = time.time()
        response = requests.get(url, timeout=15, headers=headers)
        latency = round((time.time() - start_time) * 1000)
        if response.status_code == 200:
            status = "UP"
            detail = f"{region} | {latency}ms | 200 OK"
        else:
            status = "DOWN"
            detail = f"{region} | Error: {response.status_code}"
    except Exception as e:
        status = "DOWN"
        detail = f"{region} | Connection Timeout"

    cursor = db_conn.cursor()
    cursor.execute("SELECT fail_count FROM monitors WHERE id=?", (monitor_id,))
    res = cursor.fetchone()
    if not res: return # Monitor was deleted
    fail_count = res[0]

    now = datetime.now().strftime("%H:%M:%S")
    
    # Smart Retry Logic
    final_status = status
    new_fail_count = fail_count + 1 if status == "DOWN" else 0
    
    if 0 < new_fail_count < 3:
        final_status = "UP" # Still show UP until 3 failures

    cursor.execute("UPDATE monitors SET status=?, fail_count=? WHERE id=?", (final_status, new_fail_count, monitor_id))
    cursor.execute("INSERT INTO logs (monitor_id, status, detail, timestamp) VALUES (?, ?, ?, ?)", 
                   (monitor_id, status, detail, now))
    db_conn.commit()

    if new_fail_count == 3:
        alert = f"🚨 *MONITOR DOWN*\n\n🌐 URL: {url}\n❌ Reason: {detail}\n⏰ Time: {now} UTC"
        try: bot.send_message(user_id, alert, parse_mode="Markdown")
        except: pass

# ==============================
# HELPERS
# ==============================
def get_ascii_graph(monitor_id):
    cursor = db_conn.cursor()
    cursor.execute("SELECT status FROM logs WHERE monitor_id=? ORDER BY id DESC LIMIT 20", (monitor_id,))
    rows = cursor.fetchall()
    if not rows: return "No data yet"
    history = [r[0] for r in rows][::-1]
    return "".join(["🟩" if s == 'UP' else "🟥" for s in history])

def is_verified(user_id):
    cursor = db_conn.cursor()
    cursor.execute("SELECT is_verified FROM users WHERE user_id=?", (user_id,))
    row = cursor.fetchone()
    return row and row[0] == 1

def main_menu():
    markup = types.InlineKeyboardMarkup()
    markup.row(types.InlineKeyboardButton("➕ Add Monitor", callback_data="add"),
               types.InlineKeyboardButton("📋 My List", callback_data="list"))
    return markup

# ==============================
# HANDLERS
# ==============================
@bot.message_handler(commands=['start'])
def start(message):
    uid = message.from_user.id
    if not is_verified(uid):
        cursor = db_conn.cursor()
        cursor.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (uid,))
        db_conn.commit()
        return bot.send_message(uid, "🔒 *Access Denied*\nPlease send your Access Code (AC-XXXXX) to unlock:", parse_mode="Markdown")
    
    bot.send_message(uid, "✅ *Uptime Monitor is Active*", reply_markup=main_menu(), parse_mode="Markdown")

@bot.message_handler(func=lambda m: m.text.startswith("AC-"))
def verify_code(message):
    code = message.text.strip()
    cursor = db_conn.cursor()
    cursor.execute("SELECT code FROM access_codes WHERE code=? AND is_used=0", (code,))
    if cursor.fetchone():
        cursor.execute("UPDATE access_codes SET is_used=1 WHERE code=?", (code,))
        cursor.execute("UPDATE users SET is_verified=1 WHERE user_id=?", (message.from_user.id,))
        db_conn.commit()
        bot.reply_to(message, "🎉 *Access Granted!* Type /start to begin.")
    else:
        bot.reply_to(message, "❌ Invalid or expired code.")

@bot.message_handler(commands=['admin'])
def admin_panel(message):
    if message.from_user.id != ADMIN_ID: return
    code = "AC-" + ''.join(random.choice(string.ascii_uppercase + string.digits) for _ in range(8))
    cursor = db_conn.cursor()
    cursor.execute("INSERT INTO access_codes (code) VALUES (?)", (code,))
    db_conn.commit()
    bot.send_message(ADMIN_ID, f"🔑 *New Access Code Generated:* `{code}`", parse_mode="Markdown")

@bot.callback_query_handler(func=lambda call: call.data == "add")
def ask_url(call):
    if not is_verified(call.from_user.id): return
    sent = bot.edit_message_text("🔗 Send the URL to monitor (with http/https):", call.message.chat.id, call.message.message_id)
    bot.register_next_step_handler(sent, process_url_input)

def process_url_input(message):
    url = message.text
    if not url.startswith("http"):
        return bot.send_message(message.chat.id, "❌ Error: URL must start with http or https.")
    
    cursor = db_conn.cursor()
    cursor.execute("INSERT INTO monitors (user_id, url, interval) VALUES (?, ?, ?)", (message.from_user.id, url, 0))
    db_conn.commit()
    row_id = cursor.lastrowid
    
    sent = bot.send_message(message.chat.id, "⏱ *Set Interval*\nEnter minutes (e.g. 5, 10, 60):", parse_mode="Markdown")
    bot.register_next_step_handler(sent, process_interval_input, row_id, url)

def process_interval_input(message, row_id, url):
    try:
        minutes = int(message.text)
        if minutes < 1: raise ValueError
    except:
        return bot.send_message(message.chat.id, "❌ Invalid number. Try /start again.")

    cursor = db_conn.cursor()
    cursor.execute("UPDATE monitors SET interval = ? WHERE id = ?", (minutes, row_id))
    db_conn.commit()

    scheduler.add_job(ping_url, "interval", minutes=minutes, args=[row_id, url, message.from_user.id], id=f"job_{row_id}")
    bot.send_message(message.chat.id, f"✅ *Monitoring Started*\n🌐 {url}\n⏱ Every {minutes} minutes.", parse_mode="Markdown", reply_markup=main_menu())

@bot.callback_query_handler(func=lambda call: call.data == "list")
def show_list(call):
    cursor = db_conn.cursor()
    cursor.execute("SELECT id, url, status FROM monitors WHERE user_id=? AND interval > 0", (call.from_user.id,))
    rows = cursor.fetchall()
    
    markup = types.InlineKeyboardMarkup()
    for r in rows:
        icon = "🟢" if r[2] == "UP" else "🔴" if r[2] == "DOWN" else "⚪"
        markup.add(types.InlineKeyboardButton(f"{icon} {r[1]}", callback_data=f"view_{r[0]}"))
    
    markup.add(types.InlineKeyboardButton("🔙 Back Home", callback_data="home"))
    bot.edit_message_text("📊 *Active Monitors:*", call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode="Markdown")

@bot.callback_query_handler(func=lambda call: call.data.startswith("view_"))
def view_monitor(call):
    mid = call.data.split("_")[1]
    cursor = db_conn.cursor()
    cursor.execute("SELECT url, interval, status FROM monitors WHERE id=?", (mid,))
    m = cursor.fetchone()
    
    cursor.execute("SELECT detail, timestamp FROM logs WHERE monitor_id=? ORDER BY id DESC LIMIT 5", (mid,))
    logs = cursor.fetchall()
    log_text = "\n".join([f"`[{l[1]}]` {l[0]}" for l in logs]) if logs else "No logs yet."
    
    graph = get_ascii_graph(mid)
    text = (f"🌐 *Monitor:* {m[0]}\n"
            f"⏱ *Interval:* Every {m[1]} min\n"
            f"📡 *Current Status:* {m[2]}\n\n"
            f"📊 *Uptime Graph (Last 20):*\n`{graph}`\n\n"
            f"🧭 *Live Streaming Logs:*\n{log_text}")
    
    markup = types.InlineKeyboardMarkup()
    markup.row(types.InlineKeyboardButton("🔄 Refresh", callback_data=f"view_{mid}"),
               types.InlineKeyboardButton("🗑 Delete", callback_data=f"del_{mid}"))
    markup.add(types.InlineKeyboardButton("🔙 Back to List", callback_data="list"))
    
    bot.edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode="Markdown")

@bot.callback_query_handler(func=lambda call: call.data.startswith("del_"))
def delete_monitor(call):
    mid = call.data.split("_")[1]
    cursor = db_conn.cursor()
    cursor.execute("DELETE FROM monitors WHERE id=?", (mid,))
    cursor.execute("DELETE FROM logs WHERE monitor_id=?", (mid,))
    db_conn.commit()
    
    try: scheduler.remove_job(f"job_{mid}")
    except: pass
    
    bot.answer_callback_query(call.id, "Monitor Deleted.")
    show_list(call)

@bot.callback_query_handler(func=lambda call: call.data == "home")
def go_home(call):
    bot.edit_message_text("✅ *Uptime Monitor Dashboard*", call.message.chat.id, call.message.message_id, reply_markup=main_menu(), parse_mode="Markdown")

# ==============================
# RENDER SERVER & FIXES
# ==============================
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is Running")

def run_server():
    port = int(os.environ.get("PORT", 8080))
    httpd = HTTPServer(('0.0.0.0', port), HealthHandler)
    httpd.serve_forever()

if __name__ == "__main__":
    threading.Thread(target=run_server, daemon=True).start()
    
    # Reload monitors from DB on restart
    cursor = db_conn.cursor()
    cursor.execute("SELECT id, url, interval, user_id FROM monitors WHERE interval > 0")
    for r in cursor.fetchall():
        try: scheduler.add_job(ping_url, "interval", minutes=r[2], args=[r[0], r[1], r[3]], id=f"job_{r[0]}")
        except: pass

    # --- THE CONFLICT (409) FIX ---
    bot.remove_webhook(drop_pending_updates=True)
    time.sleep(1) # Small delay to ensure Telegram sync
    
    print("Bot is alive...")
    bot.infinity_polling(timeout=60, long_polling_timeout=60)
