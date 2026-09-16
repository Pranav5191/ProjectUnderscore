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
        """Parses the CSV to calculate wins, losses, and net PnL."""
        metrics = {
            "total_executions": 0,
            "round_trips": 0,
            "winning_trades": 0,
            "losing_trades": 0,
            "net_pnl": 0.0
        }

        if not os.path.exists(self.csv_path):
            return metrics

        with open(self.csv_path, 'r') as f:
            reader = csv.DictReader(f)
            for row in reader:
                metrics["total_executions"] += 1
                
                # Booked PnL is only recorded when a position is closed
                pnl = float(row.get('booked_pnl', 0.0))
                if pnl != 0.0:
                    metrics["round_trips"] += 1
                    metrics["net_pnl"] += pnl
                    if pnl > 0:
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
                f"💵 <b>Net Realized PnL:</b> ₹{metrics['net_pnl']:,.2f}\n"
                f"━━━━━━━━━━━━━━━━━━━\n"
                f"⚡ <b>Total Executions:</b> {metrics['total_executions']}\n"
                f"🔄 <b>Closed Trades:</b> {metrics['round_trips']}\n"
                f"📈 <b>Win Rate:</b> {win_rate:.1f}%\n"
                f"✅ <b>Wins:</b> {metrics['winning_trades']} | ❌ <b>Losses:</b> {metrics['losing_trades']}\n"
            )

        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        payload = {
            "chat_id": self.chat_id,
            "text": message,
            "parse_mode": "HTML"
        }
        
        try:
            response = requests.post(url, json=payload)
            # If it fails, print Telegram's exact JSON reason instead of throwing a generic python error
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
        db_pass = os.getenv("DB_PASSWORD")
        db_name = os.getenv("DB_NAME")
        db_host = os.getenv("DB_HOST", "localhost")
        
        if not all([db_user, db_pass, db_name]):
            print("[WARNING] DB credentials missing in .env. Skipping DB backup.")
            return None

        dump_filename = f"{db_name}_backup_{self.today_str}.sql.gz"
        dump_path = os.path.join(self.base_dir, dump_filename)
        
        # Securely pass the password to the subprocess environment
        env = os.environ.copy()
        env["PGPASSWORD"] = db_pass
        
        # pg_dump piped directly into gzip to bypass Telegram's 50MB limit
        command = f"pg_dump -h {db_host} -U {db_user} -d {db_name} | gzip > {dump_path}"
        
        print(f"[SYSTEM] Initiating PostgreSQL dump for '{db_name}'...")
        try:
            subprocess.run(command, shell=True, env=env, check=True)
            print(f"[SUCCESS] Database compressed to {dump_filename}")
            return dump_path
        except subprocess.CalledProcessError as e:
            print(f"[ERROR] PostgreSQL dump failed: {e}")
            return None
        
if __name__ == "__main__":
    reporter = TelegramReporter()
    
    # 1. Send the text report
    reporter.send_report()
    
    # 2. Attach CSV and Logs
    reporter.send_file(reporter.csv_path)
    log_path = os.path.join(reporter.base_dir, "logs", f"signals_{reporter.today_str}.log")
    reporter.send_file(log_path)
    
    # 3. Dump, compress, and send the PostgreSQL Database
    db_dump_path = reporter.backup_postgres()
    if db_dump_path:
        reporter.send_file(db_dump_path)