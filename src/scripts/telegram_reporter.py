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
        
        # Target the trading directory where AWS synced the files
        self.base_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "trading")
        self.today_str = datetime.now().strftime('%Y%m%d')
        
        self.csv_path = os.path.join(self.base_dir, f"paper_trades_{self.today_str}.csv")
        self.portfolio_path = os.path.join(self.base_dir, "portfolio_state.json")

    def get_final_balance(self) -> float:
        """Reads the persistent portfolio state."""
        if os.path.exists(self.portfolio_path):
            with open(self.portfolio_path, 'r') as f:
                data = json.load(f)
                return data.get("current_balance", 100000.0)
        return 100000.0

    def parse_daily_metrics(self) -> dict:
        """Parses the CSV to calculate true net wins, losses, taxes, and PnL."""
        metrics = {
            "total_executions": 0,
            "round_trips": 0,
            "winning_trades": 0,
            "losing_trades": 0,
            "gross_pnl": 0.0,
            "total_taxes": 0.0,
            "net_pnl": 0.0
        }

        if not os.path.exists(self.csv_path):
            return metrics

        with open(self.csv_path, 'r') as f:
            reader = csv.DictReader(f)
            for row in reader:
                metrics["total_executions"] += 1
                
                # Safely extract all three financial metrics
                gross = float(row.get('gross_pnl', 0.0))
                taxes = float(row.get('transaction_taxes', 0.0))
                net = float(row.get('net_pnl', 0.0))
                
                # A trade is considered a "round trip" (closed) if it realized any gross PnL
                if gross != 0.0:
                    metrics["round_trips"] += 1
                    metrics["gross_pnl"] += gross
                    metrics["total_taxes"] += taxes
                    metrics["net_pnl"] += net
                    
                    # True wins and losses must be calculated on the NET value
                    if net > 0:
                        metrics["winning_trades"] += 1
                    else:
                        metrics["losing_trades"] += 1
                        
        return metrics

    def send_report(self):
        """Constructs the quantitative report and sends it to Telegram."""
        metrics = self.parse_daily_metrics()
        final_balance = self.get_final_balance()
        
        if metrics["total_executions"] == 0:
            message = f"📊 <b>System Report: {datetime.now().strftime('%Y-%m-%d')}</b>\n\nNo executions fired today. Engine remained flat."
        else:
            win_rate = (metrics['winning_trades'] / metrics['round_trips'] * 100) if metrics['round_trips'] > 0 else 0
            
            message = (
                f"📊 <b>EOD Quantitative Report | {datetime.now().strftime('%Y-%m-%d')}</b>\n"
                f"━━━━━━━━━━━━━━━━━━━\n"
                f"🏦 <b>Final Balance:</b> ₹{final_balance:,.2f}\n"
                f"🟢 <b>Gross PnL Made:</b> ₹{metrics['gross_pnl']:,.2f}\n"
                f"🔴 <b>Paid as Taxes and Fees:</b> ₹{metrics['total_taxes']:,.2f}\n"
                f"💵 <b>Net PnL:</b> ₹{metrics['net_pnl']:,.2f}\n"
                f"━━━━━━━━━━━━━━━━━━━\n"
                f"⚡ <b>Total Executions:</b> {metrics['total_executions']}\n"
                f"🔄 <b>Closed Trades:</b> {metrics['round_trips']}\n"
                f"📈 <b>Net Win Rate:</b> {win_rate:.1f}%\n"
                f"✅ <b>Net Wins:</b> {metrics['winning_trades']} | ❌ <b>Net Losses:</b> {metrics['losing_trades']}\n"
            )

        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        payload = {
            "chat_id": self.chat_id,
            "text": message,
            "parse_mode": "HTML"
        }
        
        try:
            response = requests.post(url, json=payload)
            if response.status_code != 200:
                print(f"[ERROR] Telegram API rejected the request: {response.text}")
            else:
                print("[SUCCESS] End-of-Day report fired to Telegram.")
        except Exception as e:
            print(f"[ERROR] Network failure: {e}")

    def send_file(self, file_path: str):
        """Pushes a file directly from the server to Telegram."""
        if not os.path.exists(file_path):
            print(f"[WARNING] File not found for upload: {file_path}")
            return
            
        url = f"https://api.telegram.org/bot{self.bot_token}/sendDocument"
        payload = {"chat_id": self.chat_id}
        
        with open(file_path, 'rb') as document:
            files = {'document': document}
            try:
                response = requests.post(url, data=payload, files=files)
                if response.status_code == 200:
                    print(f"[SUCCESS] {os.path.basename(file_path)} fired directly to Telegram.")
                else:
                    print(f"[ERROR] Telegram API rejected document: {response.text}")
            except Exception as e:
                print(f"[ERROR] Document upload failed: {e}")

    def backup_postgres(self) -> str:
        """Dumps the PostgreSQL database, gzips it, and returns the file path."""
        db_user = os.getenv("DB_USER")
        db_pass = os.getenv("DB_PASS")
        db_name = os.getenv("DB_NAME")
        db_host = os.getenv("DB_HOST", "localhost")
        
        if not all([db_user, db_pass, db_name]):
            print("[WARNING] DB credentials missing in .env. Skipping DB backup.")
            return None

        dump_filename = f"{db_name}_backup_{self.today_str}.sql.gz"
        dump_path = os.path.join(self.base_dir, dump_filename)
        
        env = os.environ.copy()
        env["PGPASSWORD"] = db_pass
        
        command = f"pg_dump -h {db_host} -U {db_user} -d {db_name} | gzip > {dump_path}"
        
        print(f"[SYSTEM] Initiating PostgreSQL dump for '{db_name}'...")
        try:
            subprocess.run(command, shell=True, env=env, check=True)
            print(f"[SUCCESS] Database compressed to {dump_filename}")
            return dump_path
        except subprocess.CalledProcessError as e:
            print(f"[ERROR] PostgreSQL dump failed: {e}")
            return None

    def send_premarket_watchlist(self, json_path):
        """Parses the generated watchlist and sends the top 5 + file to Telegram."""
        if not os.path.exists(json_path):
            print(f"[ERROR] Watchlist file not found at {json_path}")
            return
            
        try:
            with open(json_path, 'r') as f:
                data = json.load(f)
                
            if not data:
                print("[WARNING] Watchlist JSON is empty.")
                return
                
            top_5 = data[:5]
            top_5_symbols = "\n".join([f"🎯 {idx+1}. {item['symbol']} (ATR: {item['master_score']}%)" for idx, item in enumerate(top_5)])
            
            message = f"🌅 *Pre-Market Volatility Screener*\n\nTodays target ticks are:\n{top_5_symbols}"
            
            url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
            payload = {"chat_id": self.chat_id, "text": message, "parse_mode": "Markdown"}
            
            response = requests.post(url, json=payload)
            if response.status_code == 200:
                print("[SUCCESS] Pre-market Top 5 summary fired to Telegram.")
            else:
                print(f"[ERROR] Telegram text failed: {response.text}")
                
            self.send_file(json_path)
            
        except Exception as e:
            print(f"[ERROR] Failed to execute Telegram pre-market handoff: {e}")    
    def send_ai_catalyst_briefing(self, json_path: str, ai_catalyst_path: str = None):
        """Dedicated AI Catalyst & Hybrid watchlist dispatcher. Leaves all existing methods intact."""
        if not os.path.exists(json_path):
            print(f"[ERROR] Watchlist file not found at {json_path}")
            return

        try:
            with open(json_path, 'r') as f:
                data = json.load(f)

            if not data:
                print("[WARNING] Watchlist JSON is empty.")
                return

            now_str = datetime.now().strftime('%Y-%m-%d %H:%M')
            hybrid_picks = [x for x in data if x.get("cohort") == "HYBRID_MATH_AI"][:5]
            pure_picks = [x for x in data if x.get("cohort") == "PURE_AI_CATALYST"][:5]

            if not pure_picks and ai_catalyst_path and os.path.exists(ai_catalyst_path):
                with open(ai_catalyst_path, 'r') as f:
                    pure_picks = json.load(f)[:5]

            if not hybrid_picks and not pure_picks:
                hybrid_picks = data[:5]

            lines = [
                f"⚡ <b>NSE PRE-MARKET INTELLIGENCE</b> ({now_str})",
                "━━━━━━━━━━━━━━━━━━━",
                "📊 <b>TOP 5 HYBRID (MATH + CATALYST)</b>"
            ]

            for idx, p in enumerate(hybrid_picks, 1):
                sym = p.get("symbol", "").replace("-EQ", "")
                f_score = p.get("final_score", p.get("master_score", 0.0))
                m_score = p.get("master_score", 0.0)
                ai_score = p.get("ai_score", 0.50)
                lines.append(
                    f"<b>{idx}. {sym}</b> | Score: <code>{f_score:.3f}</code> "
                    f"(Math: {m_score:.2f} | AI: {ai_score:.2f})"
                )

            lines.append("\n🎯 <b>TOP 5 PURE AI CATALYSTS</b>")
            if pure_picks:
                for idx, p in enumerate(pure_picks, 1):
                    sym = p.get("symbol", "").replace("-EQ", "")
                    ai_score = p.get("ai_score", 0.50)
                    bias = p.get("sentiment_bias", "NEUTRAL")
                    reason = p.get("reasoning", "Material catalyst observed.")
                    icon = "🟢" if bias == "BULLISH" else ("🔴" if bias == "BEARISH" else "⚪")
                    lines.append(
                        f"<b>{idx}. {sym}</b> {icon} [AI: <code>{ai_score:.2f}</code>]\n"
                        f"   ↳ <i>{reason}</i>"
                    )
            else:
                lines.append("<i>Zero broad-market outliers passed liquidity scrub.</i>")

            lines.append("━━━━━━━━━━━━━━━━━━━")
            lines.append("✅ <i>Handed off to execution pipeline.</i>")

            message = "\n".join(lines)
            url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
            payload = {"chat_id": self.chat_id, "text": message, "parse_mode": "HTML"}

            response = requests.post(url, json=payload, timeout=10)
            if response.status_code == 200:
                print("[SUCCESS] Pre-market intelligence summary fired to Telegram.")
            else:
                print(f"[ERROR] Telegram text failed: {response.text}")

            self.send_file(json_path)

        except Exception as e:
            print(f"[ERROR] Failed to execute Telegram pre-market handoff: {e}")
            
if __name__ == "__main__":
    reporter = TelegramReporter()
    reporter.send_report()
    reporter.send_file(reporter.csv_path)
    log_path = os.path.join(reporter.base_dir, "logs", f"signals_{reporter.today_str}.log")
    reporter.send_file(log_path)
    db_dump_path = reporter.backup_postgres()
    if db_dump_path:
        reporter.send_file(db_dump_path)