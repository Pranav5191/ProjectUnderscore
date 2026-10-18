import os
import csv
import json
import requests
import subprocess
from datetime import datetime
from dotenv import load_dotenv

class TelegramReporter:
    def __init__(self):
        load_dotenv()
        self.bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
        self.chat_id = os.getenv("TELEGRAM_CHAT_ID")
        self.base_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "trading")
        self.today_str = datetime.now().strftime('%Y%m%d')
        self.csv_path = os.path.join(self.base_dir, f"paper_trades_{self.today_str}.csv")
        self.portfolio_path = os.path.join(self.base_dir, "portfolio_state.json")
        self.autopsy_path = os.path.join(self.base_dir, "logs", f"trade_autopsy_{self.today_str}.xlsx")

    def get_portfolio_state(self) -> dict:
        default = {
            "current_balance": 100000.0,
            "initial_balance": 100000.0,
            "daily_pnl": 0.0,
            "peak_capital_deployed": 0.0,
            "total_turnover_deployed": 0.0,
            "max_concurrent_positions": int(os.getenv("MAX_CONCURRENT_POSITIONS", 4))
        }
        if os.path.exists(self.portfolio_path):
            try:
                with open(self.portfolio_path, 'r', encoding='utf-8') as f:
                    default.update(json.load(f))
            except Exception:
                pass
        return default

    def get_final_balance(self) -> float:
        return float(self.get_portfolio_state().get("current_balance", 100000.0))

    def parse_daily_metrics(self) -> dict:
        metrics = {
            "total_orders": 0, "closed_trades": 0, "wins": 0, "losses": 0,
            "gross_pnl": 0.0, "total_taxes": 0.0, "net_pnl": 0.0,
            "per_stock": {}, "entry_sources": {}
        }
        if not os.path.exists(self.csv_path):
            return metrics

        with open(self.csv_path, 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                metrics["total_orders"] += 1
                sym = (row.get('symbol') or f"TOK_{row.get('security_id')}").replace("-EQ", "")
                gross = float(row.get('gross_pnl', 0.0))
                taxes = float(row.get('transaction_taxes', 0.0))
                net = float(row.get('net_pnl', 0.0))
                src = row.get('trigger_source', 'SYSTEM') or 'SYSTEM'

                metrics["total_taxes"] += taxes
                metrics["gross_pnl"] += gross
                metrics["net_pnl"] += net

                st = metrics["per_stock"].setdefault(sym, {"trades": 0, "gross": 0.0, "taxes": 0.0, "net": 0.0, "source": src})
                st["gross"] += gross
                st["taxes"] += taxes
                st["net"] += net

                if gross == 0.0:
                    # Entry order leg: record which brain triggered the entry
                    st["source"] = src
                    metrics["entry_sources"][src] = metrics["entry_sources"].get(src, 0) + 1
                else:
                    # Exit order leg
                    metrics["closed_trades"] += 1
                    st["trades"] += 1
                    if net > 0:
                        metrics["wins"] += 1
                    else:
                        metrics["losses"] += 1
        return metrics

    def format_eod_summary_text(self, mode_label: str = "LIVE") -> str:
        m = self.parse_daily_metrics()
        state = self.get_portfolio_state()
        init_bal = float(state.get("initial_balance", 100000.0))
        final_bal = float(state.get("current_balance", 100000.0))
        peak_notional = float(state.get("peak_capital_deployed", 0.0))
        peak_margin_cash = peak_notional / 5.0
        min_idle_cash = max(0.0, init_bal - peak_margin_cash)

        net_pnl = final_bal - init_bal
        roi_total_pct = (net_pnl / init_bal * 100.0) if init_bal > 0 else 0.0
        roi_active_pct = (net_pnl / peak_notional * 100.0) if peak_notional > 0 else 0.0
        win_rate = (m['wins'] / m['closed_trades'] * 100.0) if m['closed_trades'] > 0 else 0.0
        pnl_icon = "🟢" if net_pnl >= 0 else "🔴"

        lines = [
            f"📊 <b>END-OF-DAY TRADING REPORT ({mode_label})</b>",
            f"📅 Date: <code>{datetime.now().strftime('%d-%b-%Y')}</code>",
            "━━━━━━━━━━━━━━━━━━━━━━",
            "💰 <b>1. PROFIT & LOSS SUMMARY</b>",
            f"• Starting Balance: <b>₹{init_bal:,.2f}</b>",
            f"• Ending Balance: <b>₹{final_bal:,.2f}</b>",
            f"• Market Profit (Before Tax): <b>₹{m['gross_pnl']:+,.2f}</b>",
            f"• Brokerage & Govt Taxes: <b>-₹{m['total_taxes']:,.2f}</b>",
            f"• {pnl_icon} <b>Net Take-Home P&L: ₹{net_pnl:+,.2f} ({roi_total_pct:+.2f}%)</b>",
            "",
            "🏦 <b>2. CAPITAL USAGE (ACTIVE vs IDLE)</b>",
            f"• Max Stock Value Held at Once: <b>₹{peak_notional:,.0f}</b>",
            f"• Cash Margin Used (5x Leverage): <b>₹{peak_margin_cash:,.0f}</b>",
            f"• Idle Cash Left Untouched: <b>₹{min_idle_cash:,.0f}</b>",
            f"• Return % on Active Stock Value: <b>{roi_active_pct:+.2f}%</b>",
            "",
            "🎯 <b>3. WIN RATE & ACCURACY</b>",
            f"• Completed Trades: <b>{m['closed_trades']}</b> (Total Orders: {m['total_orders']})",
            f"• Winning Trades: <b>{m['wins']}</b> ✅ | Losing Trades: <b>{m['losses']}</b> ❌",
            f"• Win Accuracy: <b>{win_rate:.1f}%</b>",
        ]

        if m["entry_sources"]:
            lines.append("")
            lines.append("🧠 <b>4. WHO TRIGGERED TODAY'S TRADES?</b>")
            for src, cnt in m["entry_sources"].items():
                lines.append(f"• <code>{src}</code>: <b>{cnt} entries</b>")

        if m["per_stock"]:
            lines.append("")
            lines.append("📈 <b>5. STOCK-BY-STOCK P&L (NET AFTER TAX)</b>")
            sorted_stocks = sorted(m["per_stock"].items(), key=lambda x: x[1]["net"], reverse=True)
            for sym, st in sorted_stocks:
                ico = "🟢" if st["net"] >= 0 else "🔴"
                lines.append(
                    f"{ico} <code>{sym:<10}</code>: <b>₹{st['net']:+7.2f}</b> "
                    f"(Gross: ₹{st['gross']:+.0f} | {st['trades']}T | {st['source']})"
                )

        return "\n".join(lines)

    def send_report(self, mode_label: str = "LIVE"):
        message = self.format_eod_summary_text(mode_label=mode_label)
        if not self.bot_token or not self.chat_id:
            return
        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        try:
            requests.post(url, json={"chat_id": self.chat_id, "text": message, "parse_mode": "HTML"}, timeout=10)
        except Exception as e:
            print(f"[TELEGRAM WARN] Report dispatch failed: {e}")

    def send_file(self, file_path: str):
        if not file_path or not os.path.exists(file_path) or not self.bot_token:
            return
        url = f"https://api.telegram.org/bot{self.bot_token}/sendDocument"
        try:
            with open(file_path, 'rb') as document:
                requests.post(url, data={"chat_id": self.chat_id}, files={'document': document}, timeout=25)
        except Exception:
            pass

    def backup_postgres(self) -> str:
        db_user = os.getenv("DB_USER")
        db_pass = os.getenv("DB_PASS")
        db_name = os.getenv("DB_NAME")
        db_host = os.getenv("DB_HOST", "localhost")
        if not all([db_user, db_pass, db_name]):
            return None
        dump_filename = f"{db_name}_backup_{self.today_str}.sql.gz"
        dump_path = os.path.join(self.base_dir, dump_filename)
        env = os.environ.copy()
        env["PGPASSWORD"] = db_pass
        command = f"pg_dump -h {db_host} -U {db_user} -d {db_name} | gzip > {dump_path}"
        try:
            subprocess.run(command, shell=True, env=env, check=True)
            return dump_path
        except Exception:
            return None

    def send_premarket_watchlist(self, watchlist_path: str = None):
        path = watchlist_path or os.path.join(os.path.dirname(self.base_dir), "..", "target_ticks.json")
        if not os.path.exists(path) or not self.bot_token:
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                items = json.load(f)
            lines = [f"🚀 <b>Pre-Market Target Watchlist ({len(items)} Stocks)</b>\n"]
            for i, item in enumerate(items[:15], 1):
                sym = item.get("symbol", "")
                score = float(item.get("final_score", item.get("master_score", 0.0)))
                ai_s = float(item.get("ai_score", 0.50))
                cohort = item.get("cohort", "MATH")
                lines.append(f"{i}. <code>{sym:<12}</code> | Final: <b>{score:.3f}</b> (AI: {ai_s:.2f}) | {cohort}")
            url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
            requests.post(url, json={"chat_id": self.chat_id, "text": "\n".join(lines), "parse_mode": "HTML"}, timeout=10)
        except Exception as e:
            print(f"[TELEGRAM WARN] Watchlist dispatch failed: {e}")

    def send_ai_catalyst_briefing(self, watchlist_path: str = None):
        self.send_premarket_watchlist(watchlist_path)

    def dispatch(self):
        self.send_premarket_watchlist()
