import os
import asyncio
import aiohttp
import aiosqlite
import random
import string
import time
from datetime import datetime
from telebot.async_telebot import AsyncTeleBot
from telebot import types
from aiohttp import web

# ==============================
# CONFIGURATION
# ==============================
API_TOKEN = os.getenv("BOT_TOKEN")  # Render Env
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))  # Your Telegram ID

bot = AsyncTeleBot(API_TOKEN)
DB_NAME = "monitor.db"

# ==============================
# DATABASE LAYER
# ==============================
async def init_db():
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("PRAGMA journal_mode=WAL;")
        await db.execute('''CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, is_verified INTEGER DEFAULT 0)''')
        await db.execute('''CREATE TABLE IF NOT EXISTS access_codes (code TEXT PRIMARY KEY, is_used INTEGER DEFAULT 0)''')
        await db.execute('''CREATE TABLE IF NOT EXISTS monitors (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, url TEXT, 
            interval INTEGER, status TEXT DEFAULT 'UNKNOWN', last_check TEXT, fail_count INTEGER DEFAULT 0)''')
        await db.execute('''CREATE TABLE IF NOT EXISTS logs (id INTEGER PRIMARY KEY AUTOINCREMENT, monitor_id INTEGER, status TEXT, timestamp TEXT)''')
        await db.commit()

# ==============================
# UTILITIES
# ==============================
def get_ascii_graph(history):
    if not history: return "No data yet."
    # Uptime string: 🟩 for UP, 🟥 for DOWN
    return "".join(["🟩" if h == 'UP' else "🟥" for h in history[-15:]])

async def simulate_ping(url):
    regions = ["🇺🇸 US-East", "🇪🇺 EU-West", "🇸🇬 SG-Core", "🇯🇵 JP-Tokyo"]
    region = random.choice(regions)
    headers = {'User-Agent': 'MonitorBot/2.0 (Render; Cloud)'}
    try:
        start_time = time.time()
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=10, headers=headers) as resp:
                latency = round((time.time() - start_time) * 1000)
                if resp.status == 200:
                    return "UP", f"{region} | {latency}ms | 200 OK"
                return "DOWN", f"{region} | Status: {resp.status}"
    except Exception as e:
        return "DOWN", f"{region} | Timeout/Error"

# ==============================
# MONITORING ENGINE (ASYNC)
# ==============================
async def monitor_loop():
    while True:
        async with aiosqlite.connect(DB_NAME) as db:
            async with db.execute("SELECT id, user_id, url, interval, fail_count, status FROM monitors") as cursor:
                all_monitors = await cursor.fetchall()

            for mid, uid, url, interval, fail_count, old_status in all_monitors:
                # Logic: Check if it's time to ping based on interval (simplified for demo)
                status, log_msg = await simulate_ping(url)
                now = datetime.now().strftime("%H:%M:%S")

                new_fail_count = fail_count + 1 if status == "DOWN" else 0
                final_status = status

                # Smart Retry: Only mark DOWN after 3 failures
                if status == "DOWN" and new_fail_count < 3:
                    final_status = "UP"

                await db.execute("UPDATE monitors SET status=?, last_check=?, fail_count=? WHERE id=?", 
                                (final_status, now, new_fail_count, mid))
                await db.execute("INSERT INTO logs (monitor_id, status, timestamp) VALUES (?, ?, ?)", 
                                (mid, status, now))
                await db.commit()

                # Alert on 3rd failure
                if new_fail_count == 3:
                    try:
                        alert = f"🚨 *MONITOR DOWN*\n\nURL: {url}\nReason: {log_msg}\nTime: {now}"
                        await bot.send_message(uid, alert, parse_mode="Markdown")
                    except: pass
        
        await asyncio.sleep(60) # Global check cycle

# ==============================
# BOT LOGIC
# ==============================

@bot.message_handler(commands=['start'])
async def start_handler(message):
    uid = message.from_user.id
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT is_verified FROM users WHERE user_id=?", (uid,)) as cursor:
            user = await cursor.fetchone()
            if not user or user[0] == 0:
                if not user: await db.execute("INSERT INTO users (user_id) VALUES (?)", (uid,))
                await db.commit()
                return await bot.send_message(uid, "🔒 *Access Locked*\nEnter Access Code (AC-XXXX):", parse_mode="Markdown")

    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(types.InlineKeyboardButton("➕ Add", callback_data="add"),
               types.InlineKeyboardButton("📊 My Sites", callback_data="list"))
    await bot.send_message(uid, "🚀 *Professional Uptime Monitor*", reply_markup=markup, parse_mode="Markdown")

@bot.callback_query_handler(func=lambda c: c.data == "list")
async def list_monitors(call):
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT id, url, status FROM monitors WHERE user_id=?", (call.from_user.id,)) as cursor:
            mons = await cursor.fetchall()
    
    markup = types.InlineKeyboardMarkup()
    for mid, url, status in mons:
        icon = "🟢" if status == "UP" else "🔴"
        markup.add(types.InlineKeyboardButton(f"{icon} {url}", callback_data=f"view_{mid}"))
    markup.add(types.InlineKeyboardButton("🔙 Back", callback_data="home"))
    await bot.edit_message_text("🔎 *Select Monitor:*", call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode="Markdown")

@bot.callback_query_handler(func=lambda c: c.data.startswith("view_"))
async def view_monitor(call):
    mid = call.data.split("_")[1]
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT url, status, last_check, interval FROM monitors WHERE id=?", (mid,)) as cursor:
            m = await cursor.fetchone()
        async with db.execute("SELECT status FROM logs WHERE monitor_id=? ORDER BY id DESC LIMIT 15", (mid,)) as cursor:
            history = [r[0] for r in await cursor.fetchall()][::-1]

    graph = get_ascii_graph(history)
    text = (f"🌐 *Monitor:* {m[0]}\n"
            f"━━━━━━━━━━━━━━━\n"
            f"Status: {'🟢 Online' if m[1]=='UP' else '🔴 Offline'}\n"
            f"Check Interval: {m[3]}m\n"
            f"Last Ping: {m[2]}\n\n"
            f"Uptime Diagram (Last 15):\n`{graph}`")
    
    markup = types.InlineKeyboardMarkup()
    markup.row(types.InlineKeyboardButton("✏ Edit", callback_data=f"edit_{mid}"),
               types.InlineKeyboardButton("🗑 Delete", callback_data=f"del_{mid}"))
    markup.add(types.InlineKeyboardButton("🧭 Live Logs", callback_data=f"logs_{mid}"))
    markup.add(types.InlineKeyboardButton("🔙 Back", callback_data="list"))
    await bot.edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode="Markdown")

@bot.callback_query_handler(func=lambda c: c.data.startswith("logs_"))
async def live_logs(call):
    mid = call.data.split("_")[1]
    # Simple live log display (last 5)
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT status, timestamp FROM logs WHERE monitor_id=? ORDER BY id DESC LIMIT 5", (mid,)) as cursor:
            logs = await cursor.fetchall()
    
    log_text = "🧭 *Live Streaming Logs:*\n\n" + "\n".join([f"`[{l[1]}]` Status: {l[0]}" for l in logs])
    await bot.answer_callback_query(call.id, "Streaming...")
    msg = await bot.send_message(call.message.chat.id, log_text, parse_mode="Markdown")
    
    await asyncio.sleep(60)
    try: await bot.delete_message(msg.chat.id, msg.message_id) # Auto delete
    except: pass

@bot.message_handler(commands=['admin'])
async def admin_panel(message):
    if message.from_user.id != ADMIN_ID: return
    code = f"AC-{''.join(random.choice(string.ascii_uppercase + string.digits) for _ in range(6))}"
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("INSERT INTO access_codes (code) VALUES (?)", (code,))
        await db.commit()
    await bot.reply_to(message, f"🔑 *New Access Code Generated:*\n`{code}`", parse_mode="Markdown")

@bot.message_handler(func=lambda m: m.text.startswith("AC-"))
async def verify_code(message):
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT code FROM access_codes WHERE code=? AND is_used=0", (message.text,)) as cursor:
            if await cursor.fetchone():
                await db.execute("UPDATE access_codes SET is_used=1 WHERE code=?", (message.text,))
                await db.execute("UPDATE users SET is_verified=1 WHERE user_id=?", (message.from_user.id,))
                await db.commit()
                await bot.reply_to(message, "✅ Verification Successful! /start")
            else:
                await bot.reply_to(message, "❌ Invalid or Expired Code.")

@bot.callback_query_handler(func=lambda c: c.data == "add")
async def add_start(call):
    msg = await bot.send_message(call.message.chat.id, "🔗 Enter URL (including http/https):")
    bot.register_next_step_handler(msg, save_url)

async def save_url(message):
    url = message.text
    if not url.startswith("http"): return await bot.reply_to(message, "❌ Invalid URL.")
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("INSERT INTO monitors (user_id, url, interval) VALUES (?, ?, ?)", (message.from_user.id, url, 5))
        await db.commit()
    await bot.send_message(message.chat.id, "✅ Monitor Active!")

@bot.callback_query_handler(func=lambda c: c.data.startswith("del_"))
async def delete_mon(call):
    mid = call.data.split("_")[1]
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("DELETE FROM monitors WHERE id=?", (mid,))
        await db.commit()
    await bot.answer_callback_query(call.id, "Deleted!")
    await list_monitors(call)

@bot.callback_query_handler(func=lambda c: c.data == "home")
async def home(call):
    await start_handler(call.message)

# ==============================
# RENDER HEALTH CHECK
# ==============================
async def handle(request): return web.Response(text="Bot is Alive!")

async def web_server():
    app = web.Application()
    app.router.add_get('/', handle)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", 8080))
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()

# ==============================
# RUNNER
# ==============================
async def main():
    await init_db()
    asyncio.create_task(web_server())
    asyncio.create_task(monitor_loop())
    print("Bot is running...")
    await bot.polling(non_stop=True)

if __name__ == "__main__":
    asyncio.run(main())
