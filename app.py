import os
import asyncio
import aiohttp
import aiosqlite
import random
import string
from datetime import datetime
from telebot.async_telebot import AsyncTeleBot
from telebot import types
from aiohttp import web

# ==============================
# CONFIGURATION (Use Environment Variables)
# ==============================
API_TOKEN = os.getenv("BOT_TOKEN")  # Render Environment Variable এ সেট করবেন
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))  # আপনার আইডি সেট করবেন

bot = AsyncTeleBot(API_TOKEN)

# ==============================
# DATABASE SETUP
# ==============================
DB_NAME = "monitor_bot.db"

async def init_db():
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute('''CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, is_verified INTEGER DEFAULT 0)''')
        await db.execute('''CREATE TABLE IF NOT EXISTS access_codes (code TEXT PRIMARY KEY, is_used INTEGER DEFAULT 0)''')
        await db.execute('''CREATE TABLE IF NOT EXISTS monitors (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, url TEXT, 
            interval INTEGER, status TEXT DEFAULT 'UNKNOWN', last_check TEXT, fail_count INTEGER DEFAULT 0)''')
        await db.execute('''CREATE TABLE IF NOT EXISTS logs (id INTEGER PRIMARY KEY AUTOINCREMENT, monitor_id INTEGER, status TEXT, timestamp TEXT)''')
        await db.commit()

# ==============================
# UTILS
# ==============================
def generate_access_code():
    code = ''.join(random.choice(string.ascii_uppercase + string.digits) for _ in range(8))
    return f"AC-{code}"

def generate_ascii_graph(history):
    if not history: return "No data."
    return "".join(["🟩" if x == 'UP' else "🟥" for x in history[-15:]])

async def check_url(url):
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=10) as response:
                if response.status == 200:
                    return "UP", "200 OK"
                return "DOWN", f"Status: {response.status}"
    except:
        return "DOWN", "Connection Timeout"

# ==============================
# MONITORING ENGINE
# ==============================
async def monitoring_task():
    while True:
        async with aiosqlite.connect(DB_NAME) as db:
            async with db.execute("SELECT id, user_id, url, fail_count FROM monitors") as cursor:
                monitors = await cursor.fetchall()

            for m_id, u_id, url, fail_count in monitors:
                status, detail = await check_url(url)
                now = datetime.now().strftime("%H:%M:%S")
                
                final_status = status
                new_fail_count = fail_count + 1 if status == "DOWN" else 0
                
                if new_fail_count > 0 and new_fail_count < 3:
                    final_status = "UP" # Still show UP until 3rd failure

                await db.execute("UPDATE monitors SET status=?, last_check=?, fail_count=? WHERE id=?", (final_status, now, new_fail_count, m_id))
                await db.execute("INSERT INTO logs (monitor_id, status, timestamp) VALUES (?, ?, ?)", (m_id, status, now))
                await db.commit()

                if new_fail_count == 3:
                    try:
                        await bot.send_message(u_id, f"🚨 *ALERT: {url} is DOWN*\nReason: {detail}", parse_mode="Markdown")
                    except: pass
        await asyncio.sleep(60)

# ==============================
# BOT HANDLERS
# ==============================
@bot.message_handler(commands=['start'])
async def start(message):
    uid = message.from_user.id
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT is_verified FROM users WHERE user_id=?", (uid,)) as cursor:
            user = await cursor.fetchone()
            if not user or user[0] == 0:
                if not user: await db.execute("INSERT INTO users (user_id) VALUES (?)", (uid,))
                await db.commit()
                return await bot.reply_to(message, "🔐 *Access Code Required!*\nPlease send your code (AC-XXXXX):", parse_mode="Markdown")
    
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("➕ Add Monitor", callback_data="add"),
               types.InlineKeyboardButton("📊 My Sites", callback_data="list"))
    await bot.send_message(uid, "👋 *Uptime Monitor Dashboard*", reply_markup=markup, parse_mode="Markdown")

@bot.message_handler(func=lambda m: m.text.startswith("AC-"))
async def verify(message):
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT code FROM access_codes WHERE code=? AND is_used=0", (message.text,)) as cursor:
            if await cursor.fetchone():
                await db.execute("UPDATE access_codes SET is_used=1 WHERE code=?", (message.text,))
                await db.execute("UPDATE users SET is_verified=1 WHERE user_id=?", (message.from_user.id,))
                await db.commit()
                await bot.reply_to(message, "✅ Access Granted! Type /start")
            else:
                await bot.reply_to(message, "❌ Invalid or Used Code.")

@bot.message_handler(commands=['admin'])
async def admin(message):
    if message.from_user.id != ADMIN_ID: return
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("🔑 Generate Code", callback_data="gen_code"))
    await bot.send_message(ADMIN_ID, "🛠 Admin Panel", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: True)
async def callbacks(call):
    uid = call.from_user.id
    async with aiosqlite.connect(DB_NAME) as db:
        if call.data == "gen_code" and uid == ADMIN_ID:
            code = generate_access_code()
            await db.execute("INSERT INTO access_codes (code) VALUES (?)", (code,))
            await db.commit()
            await bot.answer_callback_query(call.id, f"Code: {code}", show_alert=True)
        
        elif call.data == "list":
            async with db.execute("SELECT id, url, status FROM monitors WHERE user_id=?", (uid,)) as cursor:
                mons = await cursor.fetchall()
            if not mons: return await bot.answer_callback_query(call.id, "No monitors found.")
            markup = types.InlineKeyboardMarkup()
            for m in mons:
                markup.add(types.InlineKeyboardButton(f"{'🟢' if m[2]=='UP' else '🔴'} {m[1]}", callback_data=f"v_{m[0]}"))
            await bot.edit_message_text("📊 Your Monitors:", uid, call.message.message_id, reply_markup=markup)

        elif call.data.startswith("v_"):
            mid = call.data.split("_")[1]
            async with db.execute("SELECT url, status, last_check FROM monitors WHERE id=?", (mid,)) as cursor:
                m = await cursor.fetchone()
            async with db.execute("SELECT status FROM logs WHERE monitor_id=? ORDER BY id DESC LIMIT 15", (mid,)) as cursor:
                history = [r[0] for r in await cursor.fetchall()][::-1]
            
            text = f"🌐 *URL:* {m[0]}\nStatus: {m[1]}\nLast: {m[2]}\n\nGraph: `{generate_ascii_graph(history)}`"
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("🗑 Delete", callback_data=f"del_{mid}"),
                       types.InlineKeyboardButton("🔙 Back", callback_data="list"))
            await bot.edit_message_text(text, uid, call.message.message_id, reply_markup=markup, parse_mode="Markdown")

        elif call.data.startswith("del_"):
            mid = call.data.split("_")[1]
            await db.execute("DELETE FROM monitors WHERE id=?", (mid,))
            await db.commit()
            await bot.answer_callback_query(call.id, "Deleted!")
            await bot.delete_message(uid, call.message.message_id)

        elif call.data == "add":
            msg = await bot.send_message(uid, "🔗 Send the URL (with http/https):")
            bot.register_next_step_handler(msg, save_url)

async def save_url(message):
    if not message.text.startswith("http"):
        return await bot.reply_to(message, "❌ Invalid URL.")
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("INSERT INTO monitors (user_id, url, interval) VALUES (?, ?, ?)", (message.from_user.id, message.text, 5))
        await db.commit()
    await bot.send_message(message.chat.id, "✅ Monitor Added!")

# ==============================
# RENDER WEB SERVER (Health Check)
# ==============================
async def handle(request):
    return web.Response(text="Bot is running!")

async def run_web_server():
    app = web.Application()
    app.router.add_get('/', handle)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', int(os.getenv("PORT", 10000)))
    await site.start()

# ==============================
# MAIN RUNNER
# ==============================
async def main():
    await init_db()
    await run_web_server() # Health Check for Render
    asyncio.create_task(monitoring_task())
    print("Starting Bot...")
    await bot.polling(non_stop=True)

if __name__ == "__main__":
    asyncio.run(main())
