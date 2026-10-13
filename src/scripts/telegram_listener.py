import os
import json
import asyncio
import aiohttp
import redis.asyncio as aioredis
from datetime import datetime
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

load_dotenv()
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

TIERS = {
    1: os.getenv("TIER1_CHAT_ID"),
    2: os.getenv("TIER2_CHAT_ID"),
    3: os.getenv("TIER3_CHAT_ID")
}

base_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "trading")

# Initialize Async Redis
r = aioredis.Redis(
    host=os.getenv("REDIS_HOST", "localhost"),
    port=int(os.getenv("REDIS_PORT", 6379)),
    decode_responses=True
)

# ==========================================
# 1. TWO-WAY COMMAND HANDLERS
# ==========================================

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 Trading Engine Command Node Active\n\n"
        "📊 /dashboard - Live Portfolio PnL & Positions\n"
        "📄 /logs - Get today's execution log file\n"
        "📈 /trades - Get today's CSV ledger\n"
        "🚨 /kill - EMERGENCY STOP (Flattens all positions)"
    )

async def dashboard_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Fetches live PnL and active positions instantly from Redis RAM."""
    balance = await r.get("live_balance")
    pos_data = await r.get("live_positions")
    
    if not balance:
        await update.message.reply_text("⚠️ Engine state not found in Redis. Is it running?")
        return

    current_balance = float(balance)
    net_pnl = current_balance - 100000.0 
    icon = "🟢" if net_pnl >= 0 else "🔴"
    
    msg = f"🏦 <b>Live Portfolio State</b>\nBalance: ₹{current_balance:,.2f}\n{icon} Net PnL: ₹{net_pnl:,.2f}\n\n"
    
    if not pos_data or pos_data == "{}":
        msg += "📭 Portfolio is flat (0 positions)."
    else:
        positions = json.loads(pos_data)
        msg += "📊 <b>Active Positions:</b>\n"
        for sec_id, data in positions.items():
            msg += f"• <code>{sec_id}</code> | {data['side']} | Qty: {data['qty']} @ ₹{data['avg_price']:.2f}\n"

    await update.message.reply_text(msg, parse_mode='HTML')

async def kill_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Writes the emergency override flag to Redis."""
    # Optional: Restrict this to Tier 1 Chat ID if you want security
    await r.set("EMERGENCY_STOP", "1")
    await update.message.reply_text("🚨 <b>EMERGENCY OVERRIDE ENGAGED</b>\nKill flag sent. Engine will square off immediately.", parse_mode='HTML')

async def log_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    today_str = datetime.now().strftime('%Y%m%d')
    log_path = os.path.join(base_dir, "logs", f"signals_{today_str}.log")
    if os.path.exists(log_path):
        with open(log_path, 'rb') as doc:
            await update.message.reply_document(document=doc)
    else:
        await update.message.reply_text(f"⚠️ No log file generated yet for {today_str}.")

async def csv_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    today_str = datetime.now().strftime('%Y%m%d')
    csv_path = os.path.join(base_dir, f"paper_trades_{today_str}.csv")
    if os.path.exists(csv_path):
        with open(csv_path, 'rb') as doc:
            await update.message.reply_document(document=doc)
    else:
        await update.message.reply_text("⚠️ No CSV trades recorded today.")


# ==========================================
# 2. ASYNC PUSH DISPATCHER (Background Loop)
# ==========================================

async def send_telegram_alert(session, chat_id, text):
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
    try:
        async with session.post(url, json=payload) as response:
            if response.status == 429:
                retry_after = (await response.json()).get("parameters", {}).get("retry_after", 1.0)
                await asyncio.sleep(retry_after)
                return await send_telegram_alert(session, chat_id, text)
    except Exception as e:
        print(f"[NETWORK ERROR] Failed to push alert: {e}")

async def run_dispatcher():
    """Endlessly polls Redis for alerts and pushes them to Telegram groups."""
    print("[DISPATCHER] Live Push Notification Daemon Active...")
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                # Blocks efficiently until an alert hits the queue
                result = await r.blpop("sys_alerts_queue", timeout=0)
                if not result: continue
                
                _, data_str = result
                alert = json.loads(data_str)
                
                tier = alert.get("tier", 3)
                chat_id = TIERS.get(tier)
                
                if chat_id:
                    icons = {1: "🚨", 2: "⚡", 3: "⚙️"}
                    msg = f"{icons.get(tier, '🔔')} <b>[{alert.get('source', 'SYS')}]</b>\n{alert.get('message', '')}"
                    await send_telegram_alert(session, chat_id, msg)
                
                # Strict 1-second delay prevents Telegram API bans
                await asyncio.sleep(1.0)
            except Exception as e:
                print(f"[DISPATCHER FATAL] Queue loop crashed: {e}")
                await asyncio.sleep(5)


# ==========================================
# 3. MAIN EVENT LOOP
# ==========================================

async def main():
    print("[SYSTEM] Booting Unified Telegram C2 Node...")
    app = ApplicationBuilder().token(TOKEN).build()
    
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("dashboard", dashboard_command))
    app.add_handler(CommandHandler("kill", kill_command))
    app.add_handler(CommandHandler("logs", log_command))
    app.add_handler(CommandHandler("trades", csv_command))
    
    await app.initialize()
    await app.start()
    await app.updater.start_polling()
    
    # Run the dispatcher in the background alongside the command listener
    await run_dispatcher()

if __name__ == "__main__":
    if not TOKEN:
        print("[FATAL] TELEGRAM_BOT_TOKEN missing in .env")
        exit(1)
        
    if os.name == 'nt':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        
    asyncio.run(main())
