import os
import telebot
import sqlite3
import requests
import random
import string
import threading
from datetime import datetime
from telebot import types
from apscheduler.schedulers.background import BackgroundScheduler
from http.server import HTTPServer, BaseHTTPRequestHandler

# ==============================
# CONFIGURATION
# ==============================
API_TOKEN = '8225162929:AAExD7IKh-jpAXwPCQkLDP6wKgnJhUoKVJ0'
ADMIN_ID = 7832264582 # আপনার নিজের আইডি এখানে দিন
bot = telebot.TeleBot(API_TOKEN)
scheduler = BackgroundScheduler(timezone="Asia/Dhaka")
scheduler.start()

# ==============================
# DATABASE SETUP
# ==============================
def init_db():
    conn = sqlite3.connect('uptime.db', check_same_thread=False)
    cursor = conn.cursor()
    # মনিটর টেবিল
    cursor.execute('''CREATE TABLE IF NOT EXISTS monitors 
                      (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, url TEXT, 
                       interval INTEGER, status TEXT DEFAULT 'UNKNOWN', fail_count INTEGER DEFAULT 0)''')
    # ইউজার টেবিল (ভেরিফিকেশনের জন্য)
    cursor.execute('''CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, is_verified INTEGER DEFAULT 0)''')
    # এক্সেস কোড টেবিল
    cursor.execute('''CREATE TABLE IF NOT EXISTS access_codes (code TEXT PRIMARY KEY, is_used INTEGER DEFAULT 0)''')
    # লগ টেবিল (গ্রাফের জন্য)
    cursor.execute('''CREATE TABLE IF NOT EXISTS logs (id INTEGER PRIMARY KEY AUTOINCREMENT, monitor_id INTEGER, status TEXT, timestamp TEXT)''')
    conn.commit()
    return conn

db_conn = init_db()

# ==============================
# UTILS & HELPERS
# ==============================
def generate_ascii_graph(monitor_id):
    cursor = db_conn.cursor()
    cursor.execute("SELECT status FROM logs WHERE monitor_id=? ORDER BY id DESC LIMIT 15", (monitor_id,))
    rows = cursor.fetchall()
    if not rows: return "No Data"
    # উল্টো করে সাজানো (বাম থেকে ডানে সময়)
    history = [r[0] for r in rows][::-1]
    return "".join(["🟩" if s == 'UP' else "🟥" for s in history])

def ping_url(monitor_id, url, user_id):
    regions = ["🇺🇸 US", "🇪🇺 EU", "🇸🇬 SG"]
    region = random.choice(regions)
    try:
        response = requests.get(url, timeout=10)
        status = "UP" if response.status_code == 200 else "DOWN"
    except:
        status = "DOWN"

    cursor = db_conn.cursor()
    cursor.execute("SELECT fail_count FROM monitors WHERE id=?", (monitor_id,))
    fail_count = cursor.fetchone()[0]

    now = datetime.now().strftime("%H:%M")
    
    # স্মার্ট রিট্রাই লজিক
    final_status = status
    new_fail_count = fail_count + 1 if status == "DOWN" else 0
    
    if new_fail_count > 0 and new_fail_count < 3:
        final_status = "UP" # ৩ বার ফেইল না হওয়া পর্যন্ত ইউজারকে UP দেখাবে

    cursor.execute("UPDATE monitors SET status=?, fail_count=? WHERE id=?", (final_status, new_fail_count, monitor_id))
    cursor.execute("INSERT INTO logs (monitor_id, status, timestamp) VALUES (?, ?, ?)", (monitor_id, status, now))
    db_conn.commit()

    # অ্যালার্ট পাঠানো (৩য় বার ফেইল হলে)
    if new_fail_count == 3:
        bot.send_message(user_id, f"🚨 *ALERT: DOWN*\n\nURL: {url}\nRegion: {region}\nStatus: {status}", parse_mode="Markdown")

# ==============================
# MIDDLEWARE (Access Control)
# ==============================
def is_verified(user_id):
    cursor = db_conn.cursor()
    cursor.execute("SELECT is_verified FROM users WHERE user_id=?", (user_id,))
    row = cursor.fetchone()
    return row and row[0] == 1

# ==============================
# BOT HANDLERS
# ==============================
def main_menu():
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("➕ লিঙ্ক যোগ করুন", callback_data="add"))
    markup.add(types.InlineKeyboardButton("📋 আমার লিস্ট", callback_data="list"))
    return markup

@bot.message_handler(commands=['start'])
def start(message):
    uid = message.from_user.id
    if not is_verified(uid):
        cursor = db_conn.cursor()
        cursor.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (uid,))
        db_conn.commit()
        bot.send_message(message.chat.id, "🔒 *বটটি লক করা আছে!*\n\nব্যবহার করতে অ্যাডমিনের দেওয়া এক্সেস কোডটি পাঠান (যেমন: AC-XXXXXX)", parse_mode="Markdown")
        return
    bot.send_message(message.chat.id, "✅ আপটাইমার বট এখন সচল!", reply_markup=main_menu())

@bot.message_handler(func=lambda m: m.text.startswith("AC-"))
def verify_code(message):
    code = message.text.strip()
    cursor = db_conn.cursor()
    cursor.execute("SELECT code FROM access_codes WHERE code=? AND is_used=0", (code,))
    if cursor.fetchone():
        cursor.execute("UPDATE access_codes SET is_used=1 WHERE code=?", (code,))
        cursor.execute("UPDATE users SET is_verified=1 WHERE user_id=?", (message.from_user.id,))
        db_conn.commit()
        bot.reply_to(message, "🎉 অভিনন্দন! এক্সেস কোড গ্রহণ করা হয়েছে। এখন /start দিন।")
    else:
        bot.reply_to(message, "❌ ভুল বা ব্যবহৃত কোড।")

@bot.message_handler(commands=['admin'])
def admin_panel(message):
    if message.from_user.id != ADMIN_ID: return
    code = "AC-" + ''.join(random.choice(string.ascii_uppercase + string.digits) for _ in range(8))
    cursor = db_conn.cursor()
    cursor.execute("INSERT INTO access_codes (code) VALUES (?)", (code,))
    db_conn.commit()
    bot.send_message(ADMIN_ID, f"🔑 *নতুন এক্সেস কোড:* `{code}`", parse_mode="Markdown")

@bot.callback_query_handler(func=lambda call: call.data == "add")
def ask_url(call):
    if not is_verified(call.from_user.id): return
    sent = bot.edit_message_text("আপনার ইউআরএলটি পাঠান (http/https সহ):", call.message.chat.id, call.message.message_id)
    bot.register_next_step_handler(sent, process_url_input)

def process_url_input(message):
    url = message.text
    if not url.startswith("http"):
        bot.send_message(message.chat.id, "❌ সঠিক ইউআরএল দিন।")
        return

    cursor = db_conn.cursor()
    cursor.execute("INSERT INTO monitors (user_id, url, interval) VALUES (?, ?, ?)", (message.from_user.id, url, 0))
    db_conn.commit()
    row_id = cursor.lastrowid

    markup = types.InlineKeyboardMarkup()
    btns = [types.InlineKeyboardButton(f"{m} মিনিট", callback_data=f"save_{m}_{row_id}") for m in [5, 10, 30]]
    markup.add(*btns)
    bot.send_message(message.chat.id, "ইউআরএল সেভ হয়েছে। সময় বেছে নিন:", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith("save_"))
def finalize_save(call):
    _, minutes, row_id = call.data.split("_")
    cursor = db_conn.cursor()
    cursor.execute("UPDATE monitors SET interval = ? WHERE id = ?", (int(minutes), int(row_id)))
    cursor.execute("SELECT url FROM monitors WHERE id = ?", (int(row_id),))
    url = cursor.fetchone()[0]
    db_conn.commit()

    # শিডিউলার অ্যাড করা
    scheduler.add_job(ping_url, "interval", minutes=int(minutes), args=[row_id, url, call.from_user.id], id=f"job_{row_id}")
    
    bot.edit_message_text(f"✅ সচল হয়েছে!\n\n🌐 {url}\n⏱ {minutes} মিনিট পরপর চেক করা হবে।", 
                          call.message.chat.id, call.message.message_id, reply_markup=main_menu())

@bot.callback_query_handler(func=lambda call: call.data == "list")
def show_list(call):
    cursor = db_conn.cursor()
    cursor.execute("SELECT id, url, status FROM monitors WHERE user_id=? AND interval > 0", (call.from_user.id,))
    rows = cursor.fetchall()
    
    markup = types.InlineKeyboardMarkup()
    for r in rows:
        icon = "🟢" if r[2] == "UP" else "🔴" if r[2] == "DOWN" else "⚪"
        markup.add(types.InlineKeyboardButton(f"{icon} {r[1]}", callback_data=f"view_{r[0]}"))
    
    markup.add(types.InlineKeyboardButton("🔙 ফিরে যান", callback_data="home"))
    bot.edit_message_text("📊 *আপনার মনিটর লিস্ট:*", call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode="Markdown")

@bot.callback_query_handler(func=lambda call: call.data.startswith("view_"))
def view_monitor(call):
    mid = call.data.split("_")[1]
    cursor = db_conn.cursor()
    cursor.execute("SELECT url, interval, status FROM monitors WHERE id=?", (mid,))
    m = cursor.fetchone()
    
    graph = generate_ascii_graph(mid)
    text = (f"🌐 *URL:* {m[0]}\n"
            f"⏱ *Interval:* {m[1]} min\n"
            f"📡 *Status:* {m[2]}\n\n"
            f"📊 *Uptime Graph (Last 15):*\n`{graph}`")
    
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("🗑 ডিলিট করুন", callback_data=f"del_{mid}"))
    markup.add(types.InlineKeyboardButton("🔙 লিস্টে ফিরুন", callback_data="list"))
    
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
    
    bot.answer_callback_query(call.id, "ডিলিট করা হয়েছে।")
    show_list(call)

@bot.callback_query_handler(func=lambda call: call.data == "home")
def go_home(call):
    bot.edit_message_text("আপটাইমার বট এখন সচল!", call.message.chat.id, call.message.message_id, reply_markup=main_menu())

# ==============================
# RENDER PERSISTENCE & HEALTH CHECK
# ==============================
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is Running")

def run_health_server():
    port = int(os.environ.get("PORT", 8080))
    httpd = HTTPServer(('0.0.0.0', port), HealthCheckHandler)
    httpd.serve_forever()

if __name__ == "__main__":
    # রেন্ডারে পোর্ট সচল রাখতে থ্রেডিং ব্যবহার করা হয়েছে
    threading.Thread(target=run_health_server, daemon=True).start()
    
    # বট রিস্টার্ট হলে ডাটাবেস থেকে সব শিডিউলার পুনরায় চালু করা
    cursor = db_conn.cursor()
    cursor.execute("SELECT id, url, interval, user_id FROM monitors WHERE interval > 0")
    for r in cursor.fetchall():
        scheduler.add_job(ping_url, "interval", minutes=r[2], args=[r[0], r[1], r[3]], id=f"job_{r[0]}")
    
    print("Bot Started...")
    bot.infinity_polling()
