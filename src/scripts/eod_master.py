from aws_sync import AWSSync
from telegram_reporter import TelegramReporter

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