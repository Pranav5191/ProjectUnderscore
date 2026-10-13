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

    def get_final_balance(self) -> float:
        if os.path.exists(self.portfolio_path):
            with open(self.portfolio_path, 'r') as f:
                data = json.load(f)
                return data.get("current_balance", 100000.0)
        return 100000.0

    def parse_daily_metrics(self) -> dict:
        metrics = {
            "total_executions": 0, "round_trips": 0, "winning_trades": 0,
            "losing_trades": 0, "gross_pnl": 0.0, "total_taxes": 0.0, "net_pnl": 0.0
        }
        if not os.path.exists(self.csv_path): return metrics
        with open(self.csv_path, 'r') as f:
            reader = csv.DictReader(f)
            for row in reader:
                metrics["total_executions"] += 1
                gross = float(row.get('gross_pnl', 0.0))
                taxes = float(row.get('transaction_taxes', 0.0))
                net = float(row.get('net_pnl', 0.0))
                if gross != 0.0:
                    metrics["round_trips"] += 1
                    metrics["gross_pnl"] += gross
                    metrics["total_taxes"] += taxes
                    metrics["net_pnl"] += net
                    if net > 0: metrics["winning_trades"] += 1
                    else: metrics["losing_trades"] += 1
        return metrics

    def send_report(self):
        metrics = self.parse_daily_metrics()
        final_balance = self.get_final_balance()
        if metrics["total_executions"] == 0:
            message = f"📊 <b>System Report: {datetime.now().strftime('%Y-%m-%d')}</b>\n\nNo executions fired today."
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
        requests.post(url, json={"chat_id": self.chat_id, "text": message, "parse_mode": "HTML"}, timeout=10)

    def send_file(self, file_path: str):
        if not file_path or not os.path.exists(file_path): return
        url = f"https://api.telegram.org/bot{self.bot_token}/sendDocument"
        with open(file_path, 'rb') as document:
            requests.post(url, data={"chat_id": self.chat_id}, files={'document': document}, timeout=25)

    def backup_postgres(self) -> str:
        db_user = os.getenv("DB_USER")
        db_pass = os.getenv("DB_PASS")
        db_name = os.getenv("DB_NAME")
        db_host = os.getenv("DB_HOST", "localhost")
        if not all([db_user, db_pass, db_name]): return None

        dump_filename = f"{db_name}_backup_{self.today_str}.sql.gz"
        dump_path = os.path.join(self.base_dir, dump_filename)
        env = os.environ.copy()
        env["PGPASSWORD"] = db_pass
        command = f"pg_dump -h {db_host} -U {db_user} -d {db_name} | gzip > {dump_path}"
        try:
            subprocess.run(command, shell=True, env=env, check=True)
            return dump_path
        except subprocess.CalledProcessError:
            return None