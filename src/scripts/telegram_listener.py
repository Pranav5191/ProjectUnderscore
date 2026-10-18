import os
import sys
import glob
import json
import shutil
import asyncio
import subprocess
import aiohttp
import redis.asyncio as aioredis
from datetime import datetime
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.abspath(os.path.join(current_dir, "../../"))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

load_dotenv(os.path.join(project_root, ".env"))

from src.utils.api_tracker import APITracker
from src.scripts.telegram_reporter import TelegramReporter

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
DEFAULT_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

TIERS = {
    1: os.getenv("TIER1_CHAT_ID") or DEFAULT_CHAT_ID,
    2: os.getenv("TIER2_CHAT_ID") or DEFAULT_CHAT_ID,
    3: os.getenv("TIER3_CHAT_ID") or DEFAULT_CHAT_ID,
}

base_dir = os.path.join(os.path.dirname(current_dir), "trading")
portfolio_state_path = os.path.join(base_dir, "portfolio_state.json")
bots_dir = os.path.join(project_root, "models", "bots")


def get_redis_client():
    return aioredis.Redis(
        host=os.getenv("REDIS_HOST", "localhost"),
        port=int(os.getenv("REDIS_PORT", 6379)),
        decode_responses=True,
        socket_connect_timeout=5,
        health_check_interval=30,
    )


def get_disk_state() -> dict:
    if os.path.exists(portfolio_state_path):
        try:
            with open(portfolio_state_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"current_balance": 100000.0, "initial_balance": 100000.0, "peak_capital_deployed": 0.0}


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 <b>Quantitative Trading C2 Node (Azure)</b>\n"
        "━━━━━━━━━━━━━━━━━━━\n"
        "📊 /dashboard - Active vs Idle Capital, Margin & Live Positions\n"
        "💓 /status - System Heartbeat, RAM/Disk & Process Health\n"
        "🧠 /bots - Active 25 Per-Stock Micro-Bot Profiles\n"
        "🔢 /quota - Angel One / Dhan / LLM API Quota Counter\n"
        "📈 /eod - Full EOD Capital Utilization & Attribution Report\n"
        "📄 /logs - Download today's signal log\n"
        "📑 /trades - Download today's CSV trade ledger\n"
        "🔍 /screener - Trigger EOD Quant Screener in background\n"
        "🔄 /restart - Restart live 3-pane Tmux Engine\n"
        "🚨 /kill - EMERGENCY STOP (Flattens all positions)",
        parse_mode="HTML"
    )


async def dashboard_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    r_cmd = get_redis_client()
    try:
        disk_state = get_disk_state()
        init_bal = float(disk_state.get("initial_balance", 100000.0))
        max_slots = int(os.getenv("MAX_CONCURRENT_POSITIONS", 4))

        balance = await r_cmd.get("live_balance")
        pos_data = await r_cmd.get("live_positions")
        last_tick = await r_cmd.get("last_tick_ist")
        hb = await r_cmd.get("engine_heartbeat")

        current_balance = float(balance) if balance else float(disk_state.get("current_balance", 100000.0))
        positions = json.loads(pos_data) if (pos_data and pos_data != "{}") else {}

        active_notional = sum(float(p["qty"]) * float(p["avg_price"]) for p in positions.values())
        margin_locked = active_notional / 5.0
        idle_capital = max(0.0, current_balance - margin_locked)
        peak_active = max(active_notional, float(disk_state.get("peak_capital_deployed", 0.0)))

        net_pnl = current_balance - init_bal
        roi_total = (net_pnl / init_bal * 100.0) if init_bal > 0 else 0.0
        roi_active = (net_pnl / peak_active * 100.0) if peak_active > 0 else 0.0
        icon = "🟢" if net_pnl >= 0 else "🔴"
        engine_status = "🟢 LIVE" if hb else "🌙 STANDBY"

        msg = (
            f"🏦 <b>Portfolio & Capital Dashboard ({engine_status})</b>\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"💰 <b>Total Equity:</b> ₹{current_balance:,.2f}\n"
            f"{icon} <b>Realized Net PnL:</b> ₹{net_pnl:+,.2f} ({roi_total:+.2f}%)\n"
            f"🎯 <b>ROI on Active Capital:</b> {roi_active:+.2f}%\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"⚡ <b>Active Deployed (Notional):</b> ₹{active_notional:,.2f}\n"
            f"🔒 <b>Margin Used (5x Intraday):</b> ₹{margin_locked:,.2f}\n"
            f"🟢 <b>Remaining / Idle Capital:</b> ₹{idle_capital:,.2f}\n"
            f"🔥 <b>Peak Active Capital Today:</b> ₹{peak_active:,.2f}\n"
            f"🎰 <b>Active Slots:</b> <code>{len(positions)} / {max_slots}</code>\n"
        )
        if last_tick:
            msg += f"⏱ <b>Last Tick:</b> <code>{last_tick} IST</code>\n"
        msg += "\n"

        if not positions:
            msg += "📭 <i>Portfolio is flat (100% capital idle).</i>"
        else:
            msg += "📊 <b>Open Positions:</b>\n"
            for sec_id, data in positions.items():
                sym = data.get("symbol", f"TOK_{sec_id}")
                notional = float(data["qty"]) * float(data["avg_price"])
                src = data.get("trigger_source", "BOT")
                msg += (
                    f"• <code>{sym}</code> (#{sec_id}) | <b>{data['side']}</b> {data['qty']}x "
                    f"@ ₹{data['avg_price']:.2f} (₹{notional:,.0f}) [{src}]\n"
                )

        await update.message.reply_text(msg, parse_mode="HTML")
    finally:
        await r_cmd.aclose()


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    r_cmd = get_redis_client()
    try:
        hb_raw = await r_cmd.get("engine_heartbeat")
        last_tick = await r_cmd.get("last_tick_ist") or "N/A"
        disk_free_gb = shutil.disk_usage(project_root).free / (1024 ** 3)
        hb_info = json.loads(hb_raw) if hb_raw else {}

        msg = (
            f"💓 <b>Live System Heartbeat</b>\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"• <b>Engine Mode:</b> <code>{hb_info.get('mode', 'IDLE')}</code>\n"
            f"• <b>Ticks Processed:</b> <code>{hb_info.get('ticks_processed', 0):,}</code>\n"
            f"• <b>Active Micro-Bots:</b> <code>{hb_info.get('active_bots', 0)}</code>\n"
            f"• <b>Last Tick Time:</b> <code>{last_tick} IST</code>\n"
            f"• <b>Heartbeat Wall Time:</b> <code>{hb_info.get('wall_time', 'N/A')}</code>\n"
            f"• <b>Free SSD Disk:</b> <code>{disk_free_gb:.2f} GB</code>"
        )
        await update.message.reply_text(msg, parse_mode="HTML")
    finally:
        await r_cmd.aclose()


async def bots_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    profiles = sorted(glob.glob(os.path.join(bots_dir, "profile_*.json")))
    if not profiles:
        await update.message.reply_text("⚠️ No StockMicroBot profiles found in models/bots/.")
        return
    lines = [f"🧠 <b>Calibrated StockMicroBots ({len(profiles)} Active)</b>\n"]
    for p_file in profiles[:25]:
        try:
            with open(p_file, "r", encoding="utf-8") as f:
                p = json.load(f)
            sym = p.get("symbol", "").replace("-EQ", "")
            ml_badge = "🤖ML" if p.get("has_ml_model") else "📐STAT"
            lines.append(
                f"• <code>{sym:<10}</code> ({ml_badge}) | CVD:<code>{p.get('cvd_threshold')}</code> "
                f"| SL:<code>{p.get('sl_pct', 0)*100:.1f}%</code> | AI:<code>{p.get('ai_score', 0.5):.2f}</code>"
            )
        except Exception:
            pass
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def quota_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(APITracker.get_summary_text(), parse_mode="HTML")


async def eod_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rep = TelegramReporter()
    await update.message.reply_text(rep.format_eod_summary_text(), parse_mode="HTML")


async def screener_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔍 Launching EOD Ensemble Screener in background...")
    subprocess.Popen([sys.executable, "src/scripts/screener.py"], cwd=project_root)


async def restart_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if sys.platform == "win32":
        await update.message.reply_text("ℹ️ /restart is for Linux/Azure tmux deployment.")
        return
    await update.message.reply_text("🔄 Executing restart_ws.sh (Updating symbols, bots & 3-pane tmux)...")
    subprocess.Popen(["bash", "restart_ws.sh"], cwd=project_root)


async def kill_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    r_cmd = get_redis_client()
    try:
        await r_cmd.set("EMERGENCY_STOP", "1")
        await update.message.reply_text(
            "🚨 <b>EMERGENCY OVERRIDE ENGAGED</b>\nKill flag sent. Engine will square off immediately.",
            parse_mode="HTML",
        )
    finally:
        await r_cmd.aclose()


async def log_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    today_str = datetime.now().strftime("%Y%m%d")
    log_path = os.path.join(base_dir, "logs", f"signals_{today_str}.log")
    if os.path.exists(log_path):
        with open(log_path, "rb") as doc:
            await update.message.reply_document(document=doc)
    else:
        await update.message.reply_text(f"⚠️ No log file generated yet for {today_str}.")


async def csv_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    today_str = datetime.now().strftime("%Y%m%d")
    csv_path = os.path.join(base_dir, f"paper_trades_{today_str}.csv")
    if os.path.exists(csv_path):
        with open(csv_path, "rb") as doc:
            await update.message.reply_document(document=doc)
    else:
        await update.message.reply_text("⚠️ No CSV trades recorded today.")


async def send_telegram_alert(session, chat_id, text):
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
    try:
        async with session.post(url, json=payload, timeout=10) as response:
            if response.status == 429:
                retry_after = (await response.json()).get("parameters", {}).get("retry_after", 1.0)
                await asyncio.sleep(retry_after)
                return await send_telegram_alert(session, chat_id, text)
    except Exception as e:
        print(f"[NETWORK ERROR] Failed to push alert: {e}")


async def run_dispatcher():
    print("[DISPATCHER] Live Push Notification Daemon Active...")
    r_disp = get_redis_client()
    await r_disp.set("live_balance", get_disk_state().get("current_balance", 100000.0))

    async with aiohttp.ClientSession() as session:
        while True:
            try:
                result = await r_disp.blpop("sys_alerts_queue", timeout=5)
                if not result:
                    continue
                _, data_str = result
                alert = json.loads(data_str)
                tier = int(alert.get("tier", 3))
                chat_id = TIERS.get(tier)
                if chat_id:
                    icons = {1: "🚨", 2: "⚡", 3: "⚙️"}
                    msg = f"{icons.get(tier, '🔔')} <b>[{alert.get('source', 'SYS')}]</b>\n{alert.get('message', '')}"
                    await send_telegram_alert(session, chat_id, msg)
                await asyncio.sleep(0.5)
            except Exception as e:
                print(f"[DISPATCHER WARN] Reconnecting Redis queue: {e}")
                await asyncio.sleep(3)
                r_disp = get_redis_client()


async def main():
    print("[SYSTEM] Booting Unified Telegram C2 Node...")
    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("dashboard", dashboard_command))
    app.add_handler(CommandHandler("status", status_command))
    app.add_handler(CommandHandler("bots", bots_command))
    app.add_handler(CommandHandler("quota", quota_command))
    app.add_handler(CommandHandler("eod", eod_command))
    app.add_handler(CommandHandler("screener", screener_command))
    app.add_handler(CommandHandler("restart", restart_command))
    app.add_handler(CommandHandler("kill", kill_command))
    app.add_handler(CommandHandler("logs", log_command))
    app.add_handler(CommandHandler("trades", csv_command))

    await app.initialize()
    await app.start()
    await app.updater.start_polling(drop_pending_updates=True)
    await run_dispatcher()


if __name__ == "__main__":
    if not TOKEN:
        print("[FATAL] TELEGRAM_BOT_TOKEN missing in .env")
        sys.exit(1)
    asyncio.run(main())
