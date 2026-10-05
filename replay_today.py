"""
═══════════════════════════════════════════════════════════════
  DAILY REPLAY SYSTEM — Parse SQL Backup → Redis → Strategy
═══════════════════════════════════════════════════════════════

Parses the daily .sql.gz backup, filters to ONLY the target
trading day, and fires ticks to Redis.

FAST MODE (--fast):  Writes to Redis Stream (XADD). Zero tick loss.
                     Start replayer FIRST, then strategy engine.

PACED MODE (default): Publishes to Redis Pub/Sub. Same path as live.
                      Start strategy engine FIRST, then replayer.

Usage:
    python replay_today.py                         # Paced (~10 min, pub/sub)
    python replay_today.py --fast                  # Fast (~30-60 sec, stream)
    python replay_today.py --date 20260921         # Specific date
    python replay_today.py --reset                 # Reset portfolio first
    python replay_today.py --reset --fast          # Reset + fast

Run strategy engine in another terminal:
    python -m src.trading.strategy_engine
"""

import os
import sys
import gzip
import json
import time
import argparse
import redis
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv

# Fix Windows console encoding
if sys.platform == 'win32':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

load_dotenv()

# ═══════════════════════════════════════════════════════════
# CONFIGURATION
# ═══════════════════════════════════════════════════════════

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
BACKUP_FILENAME_PATTERN = "alpha_market_data_backup_{date}.sql.gz"
REDIS_CHANNEL = "live_ticks"
REPLAY_STREAM = "replay_stream"
PORTFOLIO_STATE_FILE = os.path.join(PROJECT_ROOT, "src", "trading", "portfolio_state.json")
IST = timezone(timedelta(hours=5, minutes=30))


def find_backup_file(date_str: str, explicit_path: str = None) -> str:
    """Locates the backup file."""
    if explicit_path:
        if os.path.exists(explicit_path):
            return explicit_path
        raise FileNotFoundError(f"Specified file not found: {explicit_path}")

    filename = BACKUP_FILENAME_PATTERN.format(date=date_str)
    search_paths = [
        os.path.join(PROJECT_ROOT, filename),
        os.path.join(PROJECT_ROOT, "backups", filename),
        os.path.join(PROJECT_ROOT, "data", filename),
        os.path.join(os.path.expanduser("~"), "Downloads", filename),
    ]

    for path in search_paths:
        if os.path.exists(path):
            return path

    raise FileNotFoundError(
        f"\n[ERROR] Could not find: {filename}\n"
        f"Searched in:\n" + "\n".join(f"  > {p}" for p in search_paths) +
        f"\n\nPlace the file in the project root or use --file <path>"
    )


def get_market_hours_ms(date_str: str) -> tuple[int, int]:
    """Returns (open_ms, close_ms) for Indian market hours on the given date."""
    target_date = datetime.strptime(date_str, "%Y%m%d")
    market_open = target_date.replace(hour=9, minute=15, second=0, tzinfo=IST)
    market_close = target_date.replace(hour=15, minute=30, second=0, tzinfo=IST)
    return int(market_open.timestamp() * 1000), int(market_close.timestamp() * 1000)


def parse_sql_gz(filepath: str, date_str: str) -> list[dict]:
    """
    Parses a PostgreSQL .sql.gz dump and extracts ONLY the target date's
    market-hours ticks. No database restore needed.
    """
    open_ms, close_ms = get_market_hours_ms(date_str)
    ticks = []
    skipped = 0
    in_copy_block = False

    file_size_mb = os.path.getsize(filepath) / (1024 * 1024)
    print(f"[PARSER] Opening {os.path.basename(filepath)} ({file_size_mb:.1f} MB compressed)")
    print(f"[PARSER] Filtering to date {date_str} market hours (09:15 - 15:30 IST)")

    with gzip.open(filepath, 'rt', encoding='utf-8', errors='replace') as f:
        for line in f:
            line = line.rstrip('\n').rstrip('\r')

            if line.startswith('COPY public.market_ticks') or line.startswith('COPY market_ticks'):
                in_copy_block = True
                continue

            if in_copy_block and line == '\\.':
                in_copy_block = False
                continue

            if in_copy_block:
                try:
                    parts = line.split('\t')
                    if len(parts) < 8:
                        continue

                    ts = int(parts[0])
                    if ts < open_ms or ts > close_ms:
                        skipped += 1
                        continue

                    ticks.append({
                        "timestamp":    ts,
                        "security_id":  int(parts[1]),
                        "ltp":          float(parts[2]),
                        "ltq":          int(parts[3]),
                        "bid":          float(parts[4]),
                        "ask":          float(parts[5]),
                        "best_bid_vol": int(parts[6]),
                        "best_ask_vol": int(parts[7]),
                    })
                except (ValueError, IndexError):
                    continue

    ticks.sort(key=lambda t: t['timestamp'])

    if not ticks:
        print(f"[FATAL] No ticks found for date {date_str} (09:15-15:30 IST).")
        print(f"        Skipped {skipped:,} ticks from other dates.")
        sys.exit(1)

    print(f"[PARSER] Loaded {len(ticks):,} ticks for {date_str}")
    print(f"[PARSER] Skipped {skipped:,} ticks from other dates")
    return ticks


def reset_portfolio():
    """Resets the portfolio state to Rs.1,00,000."""
    with open(PORTFOLIO_STATE_FILE, 'w') as f:
        json.dump({"current_balance": 100000.0}, f)
    print("[RESET] Portfolio reset to Rs.1,00,000.00")


def replay_fast(ticks: list[dict], redis_client: redis.Redis):
    """
    FAST MODE: Write all ticks to a Redis Stream (XADD).
    Strategy engine reads at its own speed — zero ticks lost.
    """
    total = len(ticks)
    start_wall = time.time()

    # Clean up any old stream and set replay mode
    redis_client.delete(REPLAY_STREAM)
    redis_client.set("replay_mode", "stream")
    print(f"[REDIS] Set replay_mode=stream")

    print(f"\n{'='*55}")
    print(f"  FAST REPLAY: Writing {total:,} ticks to Redis Stream")
    print(f"{'='*55}\n")

    # Batch write using pipeline
    batch_size = 500
    pipe = redis_client.pipeline(transaction=False)

    for i, tick in enumerate(ticks):
        pipe.xadd(REPLAY_STREAM, {"data": json.dumps(tick)})

        if (i + 1) % batch_size == 0:
            pipe.execute()
            pipe = redis_client.pipeline(transaction=False)

            if (i + 1) % 25000 == 0:
                elapsed = time.time() - start_wall
                pct = ((i + 1) / total) * 100
                rate = (i + 1) / elapsed
                eta = (total - i - 1) / rate
                print(f"  [{pct:5.1f}%] {i+1:>8,} / {total:,}  "
                      f"({rate:,.0f} ticks/sec, ~{eta:.0f}s remaining)")

    # Flush remaining
    pipe.execute()

    elapsed = time.time() - start_wall
    rate = total / elapsed if elapsed > 0 else 0

    print(f"\n{'='*55}")
    print(f"  STREAM LOADED")
    print(f"  Ticks:    {total:,}")
    print(f"  Duration: {elapsed:.1f}s ({rate:,.0f} ticks/sec)")
    print(f"{'='*55}")
    print(f"\n  >> Now start the strategy engine in another terminal:")
    print(f"     python -m src.trading.strategy_engine\n")
    print(f"  The engine will read all {total:,} ticks from the stream")
    print(f"  at its own speed. Zero ticks will be lost.\n")


def replay_paced(ticks: list[dict], redis_client: redis.Redis):
    """
    PACED MODE: Publish ticks to Redis Pub/Sub (same path as live).
    Strategy engine must already be running and subscribed.
    """
    total = len(ticks)
    start_wall = time.time()

    # Make sure replay_mode is NOT set (live pub/sub path)
    redis_client.delete("replay_mode")
    redis_client.delete(REPLAY_STREAM)

    # Target ~10 minutes for a full day
    target_duration = 600.0
    batch_size = 100
    total_batches = (total + batch_size - 1) // batch_size
    delay_per_batch = target_duration / total_batches if total_batches > 0 else 0

    eta_min = target_duration / 60

    print(f"\n{'='*55}")
    print(f"  PACED REPLAY: {total:,} ticks via Pub/Sub")
    print(f"  ETA: ~{eta_min:.0f} minutes")
    print(f"  Batch: {batch_size} ticks | Delay: {delay_per_batch*1000:.1f}ms/batch")
    print(f"{'='*55}\n")

    pipe = redis_client.pipeline(transaction=False)
    for i, tick in enumerate(ticks):
        pipe.publish(REDIS_CHANNEL, json.dumps(tick))

        if (i + 1) % batch_size == 0:
            pipe.execute()
            pipe = redis_client.pipeline(transaction=False)
            time.sleep(delay_per_batch)

            if (i + 1) % 5000 == 0:
                elapsed = time.time() - start_wall
                pct = ((i + 1) / total) * 100
                rate = (i + 1) / elapsed
                eta = (total - i - 1) / rate
                eta_m = int(eta // 60)
                eta_s = int(eta % 60)
                print(f"  [{pct:5.1f}%] {i+1:>8,} / {total:,}  "
                      f"(~{eta_m}m {eta_s}s remaining)")

    # Flush remaining
    pipe.execute()

    elapsed = time.time() - start_wall
    rate = total / elapsed if elapsed > 0 else 0
    mins = int(elapsed // 60)
    secs = int(elapsed % 60)

    print(f"\n{'='*55}")
    print(f"  REPLAY COMPLETE")
    print(f"  Ticks:    {total:,}")
    print(f"  Duration: {mins}m {secs}s ({rate:,.0f} ticks/sec)")
    print(f"{'='*55}\n")


def main():
    parser = argparse.ArgumentParser(
        description="Replay daily SQL backup ticks to Redis.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python replay_today.py                     Paced replay via Pub/Sub (~10 min)
  python replay_today.py --fast              Fast replay via Stream (~30-60 sec)
  python replay_today.py --date 20260921     Replay a specific date
  python replay_today.py --reset --fast      Reset portfolio + fast replay

WORKFLOW:
  Paced mode:  Start strategy engine FIRST, then run replayer.
  Fast mode:   Run replayer FIRST, then start strategy engine.
        """
    )

    parser.add_argument("--date", type=str, default=None,
                        help="Date in YYYYMMDD format (default: today)")
    parser.add_argument("--file", type=str, default=None,
                        help="Path to a specific .sql.gz backup file")
    parser.add_argument("--fast", action="store_true",
                        help="Fast replay via Redis Stream (guaranteed delivery)")
    parser.add_argument("--reset", action="store_true",
                        help="Reset portfolio to Rs.1,00,000 before replay")
    parser.add_argument("--redis-host", type=str,
                        default=os.getenv("REDIS_HOST", "localhost"))
    parser.add_argument("--redis-port", type=int,
                        default=int(os.getenv("REDIS_PORT", 6379)))

    args = parser.parse_args()

    # ═══════════════════════════════════════════════════════
    print("\n" + "=" * 55)
    print("   DAILY REPLAY SYSTEM")
    print("=" * 55 + "\n")

    # 1. DETERMINE DATE
    date_str = args.date or datetime.now().strftime("%Y%m%d")
    print(f"[CONFIG] Target date: {date_str}")
    print(f"[CONFIG] Mode: {'FAST (Redis Stream)' if args.fast else 'PACED (Redis Pub/Sub)'}")

    # 2. FIND FILE
    try:
        filepath = find_backup_file(date_str, args.file)
    except FileNotFoundError as e:
        print(str(e))
        sys.exit(1)
    print(f"[CONFIG] File: {os.path.basename(filepath)}")

    # 3. CONNECT REDIS
    print(f"[REDIS]  Connecting to {args.redis_host}:{args.redis_port}...")
    try:
        r = redis.Redis(host=args.redis_host, port=args.redis_port, decode_responses=True)
        r.ping()
        print("[REDIS]  Connected.")
    except redis.ConnectionError as e:
        print(f"[FATAL]  Redis connection failed: {e}")
        print("         Run: docker-compose up -d redis")
        sys.exit(1)

    # 4. PARSE (single day only)
    ticks = parse_sql_gz(filepath, date_str)

    securities = set(t['security_id'] for t in ticks)
    first_time = datetime.fromtimestamp(ticks[0]['timestamp'] / 1000, tz=IST).strftime("%H:%M:%S")
    last_time = datetime.fromtimestamp(ticks[-1]['timestamp'] / 1000, tz=IST).strftime("%H:%M:%S")
    print(f"[STATS]  {len(ticks):,} ticks | {len(securities)} securities | {first_time} -> {last_time}")

    # 5. RESET PORTFOLIO
    if args.reset:
        reset_portfolio()

    # 6. REPLAY
    if args.fast:
        replay_fast(ticks, r)
    else:
        # Warn about paced mode workflow
        print("\n  ** PACED MODE: Make sure the strategy engine is already")
        print("     running in another terminal before continuing.\n")
        resp = input("  Strategy engine running? [Y/n]: ").strip().lower()
        if resp == 'n':
            print("  Start it first: python -m src.trading.strategy_engine")
            sys.exit(0)

        replay_paced(ticks, r)

    # 7. POST-REPLAY INFO
    today_str = datetime.now().strftime("%Y%m%d")
    csv_path = os.path.join(PROJECT_ROOT, "src", "trading", f"paper_trades_{today_str}.csv")

    if not args.fast:
        # For paced mode, show results now. For fast, results show after engine finishes.
        print("[RESULTS]")
        if os.path.exists(PORTFOLIO_STATE_FILE):
            with open(PORTFOLIO_STATE_FILE, 'r') as f:
                bal = json.load(f).get("current_balance", 0)
                print(f"  Balance:  Rs.{bal:,.2f}")
        if os.path.exists(csv_path):
            with open(csv_path, 'r') as f:
                trades = sum(1 for _ in f) - 1
                print(f"  Trades:   {trades}")
            print(f"  CSV:      {csv_path}")

    print("Done.\n")
    r.close()


if __name__ == "__main__":
    main()
