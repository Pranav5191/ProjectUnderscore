import os
import sys

current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.abspath(os.path.join(current_dir, "../../"))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from src.scripts.aws_sync import AWSSync
from src.scripts.telegram_reporter import TelegramReporter

def execute_reporting_pipeline():
    print("=========================================")
    print("     EOD PIPELINE INITIALIZING           ")
    print("=========================================\n")

    print("[1/2] Fetching Engine Data from AWS S3...")
    syncer = AWSSync()
    syncer.download_daily_logs()
    print("-----------------------------------------")

    print("[2/2] Parsing Metrics & Pushing to Telegram...")
    reporter = TelegramReporter()
    reporter.send_report()
    print("=========================================")
    print("           PIPELINE COMPLETE             ")
    print("=========================================")

if __name__ == "__main__":
    execute_reporting_pipeline()
