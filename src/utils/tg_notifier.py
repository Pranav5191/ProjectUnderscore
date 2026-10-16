"""
src/utils/tg_notifier.py
Lightweight Telegram Tier-3 Logging Group notifier for pipeline lifecycle events (Start / Stop / Crash).
"""
import os
import time
import shutil
import requests
from datetime import datetime
from dotenv import load_dotenv

current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.abspath(os.path.join(current_dir, "../../"))
load_dotenv(os.path.join(project_root, ".env"))

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = (
    os.getenv("TIER3_CHAT_ID")
    or os.getenv("TELEGRAM_CHAT_ID")
    or os.getenv("TELEGRAM_GROUP_ID")
    or ""
).strip()


def get_sys_snapshot() -> str:
    """Returns compact RAM & Disk status string for Telegram footers."""
    try:
        with open("/proc/meminfo", "r") as f:
            lines = {line.split(":")[0]: int(line.split()[1]) for line in f.readlines()[:5]}
        avail_mb = lines.get("MemAvailable", 0) // 1024
        total_mb = lines.get("MemTotal", 1) // 1024
        disk_free_gb = shutil.disk_usage(project_root).free / (1024**3)
        return f"RAM Free: {avail_mb}/{total_mb} MB | Disk: {disk_free_gb:.1f} GB"
    except Exception:
        return "System: Online"


def send_tg_event(stage: str, status: str, details: str = "") -> bool:
    """
    Sends a structured pipeline notification to TIER3_CHAT_ID.
    status: 'START' | 'SUCCESS' | 'WARN' | 'ERROR'
    """
    if not BOT_TOKEN or not CHAT_ID:
        print(f"[TG NOTIFIER SKIP] Missing TELEGRAM_BOT_TOKEN or TIER3_CHAT_ID in .env | {stage}: {status}")
        return False

    icons = {
        "START": "🟢 *STARTED*",
        "SUCCESS": "✅ *COMPLETED*",
        "WARN": "⚠️ *WARNING*",
        "ERROR": "🚨 *FAILED*"
    }
    badge = icons.get(status.upper(), "ℹ️ *UPDATE*")
    now_str = datetime.now().strftime("%d-%b %H:%M:%S IST")
    sys_info = get_sys_snapshot()

    text = (
        f"{badge} | *{stage}*\n"
        f"🕒 `{now_str}`\n"
    )
    if details:
        text += f"📋 {details}\n"
    text += f"💻 `{sys_info}`"

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    for attempt in range(2):
        try:
            resp = requests.post(
                url,
                json={"chat_id": CHAT_ID, "text": text, "parse_mode": "Markdown"},
                timeout=8
            )
            if resp.status_code == 200:
                return True
            else:
                print(f"[TG WARN] HTTP {resp.status_code}: {resp.text}")
        except Exception as e:
            print(f"[TG ERROR] Attempt {attempt+1}: {e}")
            time.sleep(1)
    return False


if __name__ == "__main__":
    import sys
    stg = sys.argv[1] if len(sys.argv) > 1 else "SYSTEM CHECK"
    stat = sys.argv[2] if len(sys.argv) > 2 else "SUCCESS"
    det = sys.argv[3] if len(sys.argv) > 3 else "Tier-3 Telegram logging hooks online."
    send_tg_event(stg, stat, det)
