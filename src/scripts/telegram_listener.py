import os
from datetime import datetime
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

# Load environment variables
load_dotenv()
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

# Target the exact same trading directory as your reporter
base_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "trading")

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Answers the /start command with available options."""
    text = (
        "🤖 **Trading Engine Listener Active**\n"
        "Available commands:\n"
        "/dashboard - Get current portfolio_state.json\n"
        "/log - Get today's signal execution log\n"
        "/csv - Get today's paper trades CSV"
    )
    await update.message.reply_text(text, parse_mode="Markdown")

async def dashboard_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Fetches the portfolio state."""
    portfolio_path = os.path.join(base_dir, "portfolio_state.json")
    
    if os.path.exists(portfolio_path):
        await update.message.reply_text("Fetching live dashboard state...")
        with open(portfolio_path, 'rb') as doc:
            await update.message.reply_document(document=doc)
    else:
        await update.message.reply_text("⚠️ ERROR: portfolio_state.json not found on server.")

async def log_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Fetches today's log file."""
    today_str = datetime.now().strftime('%Y%m%d')
    log_path = os.path.join(base_dir, "logs", f"signals_{today_str}.log")
    
    if os.path.exists(log_path):
        await update.message.reply_text(f"Fetching logs for {today_str}...")
        with open(log_path, 'rb') as doc:
            await update.message.reply_document(document=doc)
    else:
        await update.message.reply_text(f"⚠️ ERROR: No log file generated yet for {today_str}.")

async def csv_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Fetches today's trade CSV."""
    today_str = datetime.now().strftime('%Y%m%d')
    csv_path = os.path.join(base_dir, f"paper_trades_{today_str}.csv")
    
    if os.path.exists(csv_path):
        await update.message.reply_text(f"Fetching paper trades for {today_str}...")
        with open(csv_path, 'rb') as doc:
            await update.message.reply_document(document=doc)
    else:
        await update.message.reply_text("⚠️ ERROR: No CSV trades recorded today.")

if __name__ == "__main__":
    if not TOKEN:
        print("[FATAL] TELEGRAM_BOT_TOKEN missing in .env")
        exit(1)
        
    print("[SYSTEM] Booting Async Telegram Listener...")
    
    # Initialize the Application (The Event Loop)
    app = ApplicationBuilder().token(TOKEN).build()
    
    # Map the /commands to their respective async functions
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("dashboard", dashboard_command))
    app.add_handler(CommandHandler("logs", log_command))
    app.add_handler(CommandHandler("trades", csv_command))
    
    print("[SUCCESS] Listener is active. Send /start to the bot.")
    
    # Keeps the script running 24/7 listening for updates
    app.run_polling()