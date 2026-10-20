"""
═══════════════════════════════════════════════════════════════
  FULL-PIPELINE REPLAY SYSTEM (Bots -> Redis -> Strategy Engine)
═══════════════════════════════════════════════════════════════
Usage:
  python replay_today.py --date 20261015 --reset --full     # Full 1-command pipeline (Bots + Stream + Auto-Run Engine)
  python replay_today.py --date 20261015 --reset --fast     # Load Stream only
  python replay_today.py --file path/to/backup.sql.gz --full
"""
import os
import sys
import csv
import gzip
import json
import time
import subprocess
import argparse
import redis
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv
import sqlite3
import numpy as np
import pandas as pd
import lightgbm as lgb
from src.ml.bot_trainer import build_sqlite_fallback_profile, FEATURE_COLS, fetch_60d_1m_candles, engineer_2m_features
from src.auth.angel_auth import AngelAuthenticator


if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

load_dotenv()

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
BACKUP_FILENAME_PATTERN = "alpha_market_data_backup_{date}.sql.gz"
REDIS_CHANNEL = "live_ticks"
REPLAY_STREAM = "replay_stream"
PORTFOLIO_STATE_FILE = os.path.join(PROJECT_ROOT, "src", "trading", "portfolio_state.json")
BOTS_DIR = os.path.join(PROJECT_ROOT, "models", "bots")
TARGET_TICKS_FILE = os.path.join(PROJECT_ROOT, "target_ticks.json")
IST = timezone(timedelta(hours=5, minutes=30))
EXCLUDED_TOKENS = {"99926000", "99926009", "99926002", "26000", "26009", "26014", "2885", "1333"}


def find_backup_file(date_str: str, explicit_path: str = None) -> str:
    if explicit_path:
        if os.path.exists(explicit_path):
            return explicit_path
        raise FileNotFoundError(f"Specified file not found: {explicit_path}")

    filename = BACKUP_FILENAME_PATTERN.format(date=date_str)
    search_paths = [
        os.path.join(PROJECT_ROOT, filename),
        os.path.join(PROJECT_ROOT, "src", "trading", filename),
        os.path.join(PROJECT_ROOT, "backups", filename),
        os.path.join(PROJECT_ROOT, "data", filename),
        os.path.join(os.path.expanduser("~"), "Downloads", filename),
    ]
    for path in search_paths:
        if os.path.exists(path):
            return path

    # Auto-discover any available .sql.gz if date wasn't explicitly found
    avail = []
    for root_dir in [PROJECT_ROOT, os.path.join(PROJECT_ROOT, "src", "trading"), os.path.join(PROJECT_ROOT, "backups")]:
        if os.path.exists(root_dir):
            for f in os.listdir(root_dir):
                if f.endswith(".sql.gz"):
                    avail.append(os.path.join(root_dir, f))

    hint = "\nAvailable backups found:\n  " + "\n  ".join(avail) if avail else "\nNo .sql.gz files found locally."
    raise FileNotFoundError(f"[ERROR] Could not find {filename}.{hint}\nUse: python replay_today.py --file <path> --full")


def get_market_hours_ms(date_str: str) -> tuple[int, int]:
    target_date = datetime.strptime(date_str, "%Y%m%d")
    market_open = target_date.replace(hour=9, minute=15, second=0, tzinfo=IST)
    market_close = target_date.replace(hour=15, minute=30, second=0, tzinfo=IST)
    return int(market_open.timestamp() * 1000), int(market_close.timestamp() * 1000)


def parse_sql_gz(filepath: str, date_str: str = None) -> tuple[list[dict], str]:
    open_ms, close_ms = get_market_hours_ms(date_str) if date_str else (0, float("inf"))
    ticks = []
    in_copy_block = False

    file_size_mb = os.path.getsize(filepath) / (1024 * 1024)
    print(f"[PARSER] Reading {os.path.basename(filepath)} ({file_size_mb:.1f} MB)...")

    with gzip.open(filepath, "rt", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.rstrip("\r\n")
            if line.startswith("COPY public.market_ticks") or line.startswith("COPY market_ticks"):
                in_copy_block = True
                continue
            if in_copy_block and line == "\\.":
                in_copy_block = False
                continue
            if in_copy_block:
                parts = line.split("\t")
                if len(parts) < 8:
                    continue
                try:
                    ts = int(parts[0])
                    if date_str and (ts < open_ms or ts > close_ms):
                        continue
                    ticks.append({
                        "timestamp": ts,
                        "security_id": int(parts[1]),
                        "ltp": float(parts[2]),
                        "ltq": int(parts[3]),
                        "bid": float(parts[4]) or float(parts[2]),
                        "ask": float(parts[5]) or float(parts[2]),
                        "best_bid_vol": int(parts[6]),
                        "best_ask_vol": int(parts[7]),
                    })
                except ValueError:
                    continue

    # If date filter matched 0 ticks (e.g. backup contains a different date), auto-detect the primary trading date in the dump
    if not ticks and date_str:
        print(f"[PARSER] No ticks matched {date_str}. Auto-detecting trading date inside {os.path.basename(filepath)}...")
        return parse_sql_gz(filepath, date_str=None)

    if not ticks:
        print("[FATAL] Backup file contains 0 valid ticks.")
        sys.exit(1)

    ticks.sort(key=lambda t: t["timestamp"])
    detected_date = datetime.fromtimestamp(ticks[-1]["timestamp"] / 1000, tz=IST).strftime("%Y%m%d")
    if not date_str:
        open_ms, close_ms = get_market_hours_ms(detected_date)
        ticks = [t for t in ticks if open_ms <= t["timestamp"] <= close_ms]

    print(f"[PARSER] Loaded {len(ticks):,} market-hour ticks for trading date {detected_date}")
    return ticks, detected_date


def ensure_bots_for_replay(security_ids: set[int], ticks: list[dict] = None):
    """
    Compiles native LightGBM bot_<token>.txt models and pre-seeds 15 warmup_bars
    into profile_<token>.json (using SQLite history + intraday tick bars) so
    StockMicroBot ML inference is active from 09:15:00 AM.
    """
    sc = None
    os.makedirs(BOTS_DIR, exist_ok=True)
    db_path = os.path.join(PROJECT_ROOT, "master_history.db")

    sym_map = {}
    if os.path.exists(db_path):
        try:
            conn = sqlite3.connect(db_path)
            for tok_r, sym_r in conn.execute("SELECT DISTINCT token, symbol FROM historical_data").fetchall():
                sym_map[str(tok_r)] = str(sym_r)
            conn.close()
        except Exception:
            pass

    target_map = {}
    if os.path.exists(TARGET_TICKS_FILE):
        try:
            with open(TARGET_TICKS_FILE, "r", encoding="utf-8") as f:
                for item in json.load(f):
                    target_map[str(item.get("token"))] = item
        except Exception:
            pass

    # Build 2-minute bars per stock from the tick stream for instant warmup & fallback LightGBM training
    tick_bars_by_sec = {}
    stock_prices = {}
    if ticks:
        for t in ticks:
            sid = str(t["security_id"])
            if sid in EXCLUDED_TOKENS:
                continue
            ltp = float(t["ltp"])
            ltq = int(t["ltq"])
            if ltp <= 0:
                continue
            stock_prices[sid] = ltp
            bucket = (int(t["timestamp"]) // 1000) // 60 * 60  # 1-min buckets for training richness
            bars_dict = tick_bars_by_sec.setdefault(sid, {})
            b = bars_dict.get(bucket)
            if b is None:
                bars_dict[bucket] = {"open": ltp, "high": ltp, "low": ltp, "close": ltp, "vol": max(1, ltq)}
            else:
                if ltp > b["high"]: b["high"] = ltp
                if ltp < b["low"]: b["low"] = ltp
                b["close"] = ltp
                b["vol"] += max(0, ltq)

    calibrated = 0
    ml_ready = 0
    for sec_id in sorted(security_ids):
        tok = str(sec_id)
        if tok in EXCLUDED_TOKENS:
            continue

        prof_path = os.path.join(BOTS_DIR, f"profile_{tok}.json")
        model_path = os.path.join(BOTS_DIR, f"bot_{tok}.txt")

        sym_name = target_map.get(tok, {}).get("symbol") or sym_map.get(tok) or f"TOK_{tok}"
        item = target_map.get(tok, {"token": tok, "symbol": sym_name, "master_score": 0.65, "ai_score": 0.55})
        item["symbol"] = sym_name
        prof = build_sqlite_fallback_profile(tok, item)

        p_now = float(stock_prices.get(tok, prof.get("median_price", 500.0)))
        prof["median_price"] = round(p_now, 2)
        prof["cvd_threshold"] = round(max(50.0, 100000.0 / max(p_now, 1.0)), 1)
        prof["obi_threshold"] = 0.20
        prof["sl_pct"] = 0.012
        prof["tp_pct"] = 0.024
        prof["breakeven_pct"] = 0.0075
        prof["partial_tp_pct"] = 0.012
        prof["vwap_buffer_pct"] = 0.0020
        if not (os.path.exists(model_path) and os.path.getsize(model_path) > 500):
            try:
                if sc is None: auth = AngelAuthenticator(); auth.generate_session(); sc = auth.smart_connect
                end_dt = datetime.fromtimestamp(ticks[0]["timestamp"] / 1000, tz=IST) if ticks else datetime.now()
                df_2m = engineer_2m_features(fetch_60d_1m_candles(sc, tok, end_dt))
                if len(df_2m) >= 150:
                    fwd = (df_2m["close"].shift(-8) - df_2m["close"]) / df_2m["close"]
                    y = np.select([fwd.iloc[:-8] < -0.004, fwd.iloc[:-8] > 0.004], [0, 2], default=1).astype(int)
                    lgb.train({"objective": "multiclass", "num_class": 3, "max_depth": 4, "num_leaves": 15, "learning_rate": 0.05, "verbose": -1, "seed": 42}, lgb.Dataset(df_2m[FEATURE_COLS].iloc[:-8].values.astype(np.float32), label=y, feature_name=FEATURE_COLS), num_boost_round=60).save_model(model_path)
                    prof["warmup_bars"] = df_2m[["open", "high", "low", "close", "vol"]].tail(15).to_dict("records")
            except Exception as e:
                print(f"  [WARN] Angel API 60d train skipped for {tok}: {e}")
        prof["has_ml_model"] = os.path.exists(model_path) and os.path.getsize(model_path) > 500

        if prof["has_ml_model"]:
            ml_ready += 1

        with open(prof_path, "w", encoding="utf-8") as pf:
            json.dump(prof, pf, indent=4)
        calibrated += 1

    print(f"[BOT CHECK] Calibrated {calibrated} StockMicroBots ({ml_ready} LightGBM ML Models + Warmup Bars Ready).")


def load_stream(ticks: list[dict], r: redis.Redis):
    total = len(ticks)
    t0 = time.time()
    r.delete(REPLAY_STREAM)
    r.set("replay_mode", "stream")

    pipe = r.pipeline(transaction=False)
    for i, tick in enumerate(ticks):
        pipe.xadd(REPLAY_STREAM, {"data": json.dumps(tick)})
        if (i + 1) % 1000 == 0:
            pipe.execute()
            pipe = r.pipeline(transaction=False)
    pipe.execute()
    elapsed = max(0.001, time.time() - t0)
    print(f"[STREAM READY] Loaded {total:,} ticks into Redis Stream in {elapsed:.1f}s ({total/elapsed:,.0f} ticks/sec)")


def print_post_replay_summary():
    today_str = datetime.now().strftime("%Y%m%d")
    csv_path = os.path.join(PROJECT_ROOT, "src", "trading", f"paper_trades_{today_str}.csv")
    state = {"current_balance": 100000.0, "initial_balance": 100000.0, "peak_capital_deployed": 0.0, "total_turnover_deployed": 0.0}
    if os.path.exists(PORTFOLIO_STATE_FILE):
        with open(PORTFOLIO_STATE_FILE, "r", encoding="utf-8") as f:
            state.update(json.load(f))

    bal = float(state.get("current_balance", 100000.0))
    init_bal = float(state.get("initial_balance", 100000.0))
    peak_cap = float(state.get("peak_capital_deployed", 0.0))
    turnover = float(state.get("total_turnover_deployed", 0.0))
    net_pnl = bal - init_bal
    roi_active = (net_pnl / peak_cap * 100.0) if peak_cap > 0 else 0.0

    print("\n" + "=" * 65)
    print("  FULL-PIPELINE REPLAY & CAPITAL UTILIZATION SUMMARY")
    print("=" * 65)
    print(f"  Final Portfolio Balance : Rs.{bal:,.2f} (Net PnL: Rs.{net_pnl:+,.2f} | {net_pnl/init_bal*100:+.2f}%)")
    print(f"  Peak Active Capital     : Rs.{peak_cap:,.2f} (ROI on Active Cap: {roi_active:+.2f}%)")
    print(f"  Total Turnover Deployed : Rs.{turnover:,.2f}")
    if os.path.exists(csv_path):
        with open(csv_path, "r", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        closed = [r for r in rows if float(r.get("gross_pnl", 0)) != 0]
        wins = [r for r in closed if float(r.get("net_pnl", 0)) > 0]
        gross_sum = sum(float(r.get("gross_pnl", 0)) for r in rows)
        tax_sum = sum(float(r.get("transaction_taxes", 0)) for r in rows)
        print(f"  Gross PnL vs Taxes      : Gross Rs.{gross_sum:+,.2f} | Taxes -Rs.{tax_sum:,.2f}")
        print(f"  Total Executions (Legs) : {len(rows)}")
        print(f"  Closed Round-Trips      : {len(closed)} (Wins: {len(wins)} | Win Rate: {(len(wins)/len(closed)*100) if closed else 0:.1f}%)")
        print(f"  CSV Ledger              : {csv_path}")
    print("=" * 65 + "\n")


def main():
    parser = argparse.ArgumentParser(description="Full-Pipeline Replay System")
    parser.add_argument("--date", type=str, nargs="?", const=None, default=None, help="Date in YYYYMMDD format")
    parser.add_argument("--file", type=str, default=None, help="Explicit path to .sql.gz backup")
    parser.add_argument("--fast", action="store_true", help="Load Redis stream for fast replay")
    parser.add_argument("--full", action="store_true", help="Run full pipeline (Ensure Bots -> Load Stream -> Run Strategy Engine)")
    parser.add_argument("--train-bots", action="store_true", help="Force live 60-day Angel API bot training before replay")
    parser.add_argument("--reset", action="store_true", help="Reset portfolio to Rs.1,00,000 and clear today's trade CSV")
    args = parser.parse_args()

    date_str = args.date or datetime.now().strftime("%Y%m%d")
    filepath = find_backup_file(date_str, args.file)

    r = redis.Redis(host=os.getenv("REDIS_HOST", "localhost"), port=int(os.getenv("REDIS_PORT", 6379)), decode_responses=True)
    r.ping()

    ticks, detected_date = parse_sql_gz(filepath, args.date)
    securities = set(t["security_id"] for t in ticks)

    if args.reset:
        with open(PORTFOLIO_STATE_FILE, "w") as f:
            json.dump({"current_balance": 100000.0}, f)
        today_csv = os.path.join(PROJECT_ROOT, "src", "trading", f"paper_trades_{datetime.now().strftime('%Y%m%d')}.csv")
        if os.path.exists(today_csv):
            os.remove(today_csv)
        print("[RESET] Portfolio reset to Rs.1,00,000.00 & cleared today's paper_trades CSV.")

    if args.train_bots:
        subprocess.run([sys.executable, "-m", "src.ml.bot_trainer"], check=False)

    ensure_bots_for_replay(securities, ticks)

    load_stream(ticks, r)
    if args.fast and not args.full:
        print("\n>> Now run: python -m src.trading.strategy_engine\n")
    else:
        print("\n[AUTO-RUN] Launching Strategy Engine on Replay Stream...\n")
        subprocess.run([sys.executable, "-m", "src.trading.strategy_engine"], check=False)
        print_post_replay_summary()


if __name__ == "__main__":
    main()
