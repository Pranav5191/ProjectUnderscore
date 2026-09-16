import os
import boto3
from datetime import datetime
from dotenv import load_dotenv

class AWSSync:
    def __init__(self):
        load_dotenv()
        self.bucket_name = os.getenv("S3_BUCKET_NAME")
        
        # Initialize Boto3 client using environment variables
        self.s3 = boto3.client(
            's3',
            aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID"),
            aws_secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY"),
            region_name=os.getenv("AWS_REGION", "ap-south-1")
        )
        
        self.local_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "trading")
        self.today_str = datetime.now().strftime('%Y%m%d')

    def download_daily_logs(self):
        """Fetches the current day's CSV ledger and Signal Log from AWS S3."""
        files_to_sync = [
            f"paper_trades_{self.today_str}.csv",
            f"logs/signals_{self.today_str}.log",
            "portfolio_state.json"
        ]

        print(f"[AWS SYNC] Connecting to S3 Bucket: {self.bucket_name}...")

        for file_key in files_to_sync:
            local_path = os.path.join(self.local_dir, file_key)
            
            # Ensure local subdirectories exist (like /logs)
            os.makedirs(os.path.dirname(local_path), exist_ok=True)

            try:
                self.s3.download_file(self.bucket_name, file_key, local_path)
                print(f"[SUCCESS] Downloaded {file_key} to {local_path}")
            except Exception as e:
                print(f"[ERROR] Failed to download {file_key}. Reason: {e}")

    def upload_daily_logs(self):
        """Pushes the current day's CSV, Log, and Portfolio State to AWS S3."""
        files_to_sync = [
            f"paper_trades_{self.today_str}.csv",
            f"logs/signals_{self.today_str}.log",
            "portfolio_state.json"  # State memory must be pushed
        ]

        print(f"\n[AWS SYNC] Pushing daily files to S3 Bucket: {self.bucket_name}...")

        for file_name in files_to_sync:
            local_path = os.path.join(self.local_dir, file_name)
            
            if os.path.exists(local_path):
                try:
                    self.s3.upload_file(local_path, self.bucket_name, file_name)
                    print(f"[SUCCESS] Uploaded {file_name} to S3.")
                except Exception as e:
                    print(f"[ERROR] Failed to upload {file_name}. Reason: {e}")
            else:
                print(f"[WARNING] Local file not found: {local_path}")

if __name__ == "__main__":
    syncer = AWSSync()
    syncer.download_daily_logs()