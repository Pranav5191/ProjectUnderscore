"""
src/ml/bot_trainer.py
Dynamic Per-Stock Micro-Bot Factory (60-Day / 2-Month Two-Chunk Edition).
Fetches 60 days of 1-minute data in two 30-day windows per stock, resamples to 2-minute bars,
trains 25 custom LightGBM models + 25 microstructure profiles, and sends Telegram start/stop pings.
"""
import os
import sys
import json
import time
import sqlite3
import traceback
import redis
import numpy as np
import pandas as pd
import lightgbm as lgb
from datetime import datetime, timedelta
from dotenv import load_dotenv
from sklearn.utils.class_weight import compute_sample_weight

current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.abspath(os.path.join(current_dir, "../../"))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

load_dotenv(os.path.join(project_root, ".env"))

from src.auth.angel_auth import AngelAuthenticator
from src.utils.tg_notifier import send_tg_event

BOTS_DIR = os.path.join(project_root, "models", "bots")
TARGET_TICKS_PATH = os.path.join(project_root, "target_ticks.json")
DB_PATH = os.path.join(project_root, "master_history.db")
MACRO_CSV_PATH = os.path.join(project_root, "historical_macro_matrix.csv")

os.makedirs(BOTS_DIR, exist_ok=True)

FEATURE_COLS = [
    "ret_2m", "ret_6m", "ret_16m",
    "vwap_dist_atr", "rvol_2m", "body_ratio",
    "atr_pct", "rsi_14"
]


def seed_macro_redis():
    """Pre-seeds macro_features_latest in Redis so 09:15 AM has zero cold-start NaNs."""
    if not os.path.exists(MACRO_CSV_PATH):
        return
    try:
        df = pd.read_csv(MACRO_CSV_PATH).tail(5)
        if not df.empty:
            latest = df.iloc[-1].fillna(0.0).to_dict()
            latest.pop("timestamp", None)
            r = redis.Redis(
                host=os.getenv("REDIS_HOST", "localhost"),
                port=int(os.getenv("REDIS_PORT", 6379)),
                decode_responses=True
            )
            r.set("macro_features_latest", json.dumps(latest))
            print("[MACRO SEED] Pre-loaded latest macro_features_latest into Redis.")
    except Exception as e:
        print(f"[MACRO SEED WARN] {e}")


def fetch_60d_1m_candles(sc, token: str, end_dt: datetime) -> pd.DataFrame:
    """
    Fetches 60 days (2 months) of ONE_MINUTE candles using two 30-day API chunks
    to respect Angel One's 30-day max window per request.
    """
    mid_dt = end_dt - timedelta(days=30)
    start_dt = end_dt - timedelta(days=60)

    windows = [
        (start_dt.strftime("%Y-%m-%d 09:15"), mid_dt.strftime("%Y-%m-%d 15:30")),
        ((mid_dt + timedelta(minutes=1)).strftime("%Y-%m-%d 09:15"), end_dt.strftime("%Y-%m-%d 15:30"))
    ]

    all_rows = []
    for from_str, to_str in windows:
        params = {
            "exchange": "NSE",
            "symboltoken": str(token),
            "interval": "ONE_MINUTE",
            "fromdate": from_str,
            "todate": to_str
        }
        resp = sc.getCandleData(params)
        time.sleep(0.42)  # Respect 3 req/sec Angel One rate limit
        if isinstance(resp, dict) and resp.get("data"):
            all_rows.extend(resp["data"])

    if not all_rows:
        return pd.DataFrame()

    df_1m = pd.DataFrame(all_rows, columns=["timestamp", "open", "high", "low", "close", "vol"])
    df_1m["timestamp"] = pd.to_datetime(df_1m["timestamp"])
    df_1m.drop_duplicates(subset=["timestamp"], keep="last", inplace=True)
    df_1m.sort_values("timestamp", inplace=True)
    df_1m.set_index("timestamp", inplace=True)
    for c in ["open", "high", "low", "close", "vol"]:
        df_1m[c] = df_1m[c].astype(float)
    return df_1m


def build_sqlite_fallback_profile(token: str, item: dict) -> dict:
    """Builds a calibrated profile from master_history.db if intraday API is unavailable."""
    price = 500.0
    daily_vol = 500000.0
    daily_atr_pct = 0.025
    if os.path.exists(DB_PATH):
        try:
            conn = sqlite3.connect(DB_PATH)
            df = pd.read_sql_query(
                "SELECT open, high, low, close, volume FROM historical_data WHERE token = ? ORDER BY timestamp DESC LIMIT 40",
                conn, params=(str(token),)
            )
            conn.close()
            if not df.empty:
                price = float(df["close"].iloc[0])
                daily_vol = float(df["volume"].mean())
                tr = (df["high"] - df["low"]) / df["close"].replace(0, 1.0)
                daily_atr_pct = float(tr.mean())
        except Exception:
            pass

    est_2m_vol = max(100.0, daily_vol / 188.0)
    est_2m_atr_pct = max(0.0015, daily_atr_pct / np.sqrt(188.0))
    sl_pct = float(np.clip(daily_atr_pct * 0.45, 0.006, 0.018))
    ai_score = float(item.get("ai_score", 0.50))

    return {
        "token": str(token),
        "symbol": item.get("symbol", f"TOK_{token}"),
        "cohort": item.get("cohort", "MATH"),
        "ai_score": round(ai_score, 4),
        "master_score": round(float(item.get("master_score", 0.50)), 4),
        "direction_bias": item.get("direction_bias", "NEUTRAL"),
        "median_price": round(price, 2),
        "atr_2m_inr": round(price * est_2m_atr_pct, 4),
        "atr_2m_pct": round(est_2m_atr_pct, 5),
        "cvd_threshold": round(float(np.clip(est_2m_vol * 0.12, 30.0, 2500.0)), 1),
        "obi_threshold": 0.18 if ai_score >= 0.65 else 0.22,
        "sl_pct": round(sl_pct, 4),
        "tp_pct": round(sl_pct * 2.0, 4),
        "breakeven_pct": round(sl_pct * 0.65, 4),
        "partial_tp_pct": round(sl_pct * 1.0, 4),
        "vwap_buffer_pct": round(float(np.clip(est_2m_atr_pct * 0.8, 0.001, 0.0035)), 4),
        "has_ml_model": False
    }


def engineer_2m_features(df_1m: pd.DataFrame) -> pd.DataFrame:
    """Resamples 1-minute candles to 2-minute bars and computes the 8 stock features."""
    df_2m = df_1m.resample("2min", closed="left", label="left").agg({
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
        "vol": "sum"
    }).dropna()

    if len(df_2m) < 60:
        return pd.DataFrame()

    df_2m["ret_2m"] = df_2m["close"].pct_change(1)
    df_2m["ret_6m"] = df_2m["close"].pct_change(3)
    df_2m["ret_16m"] = df_2m["close"].pct_change(8)

    df_2m["date"] = df_2m.index.date
    df_2m["pv"] = df_2m["close"] * df_2m["vol"]
    cum_pv = df_2m.groupby("date")["pv"].cumsum()
    cum_vol = df_2m.groupby("date")["vol"].cumsum().replace(0, 1.0)
    df_2m["vwap"] = cum_pv / cum_vol

    prev_c = df_2m["close"].shift(1)
    tr = pd.concat([
        df_2m["high"] - df_2m["low"],
        (df_2m["high"] - prev_c).abs(),
        (df_2m["low"] - prev_c).abs()
    ], axis=1).max(axis=1)
    df_2m["atr_14"] = tr.rolling(14).mean().bfill()
    df_2m["atr_pct"] = (df_2m["atr_14"] / df_2m["close"].replace(0, 1.0)).clip(0.0005, 0.05)
    df_2m["vwap_dist_atr"] = ((df_2m["close"] - df_2m["vwap"]) / df_2m["atr_14"].replace(0, 1e-4)).clip(-5.0, 5.0)

    vol_sma = df_2m["vol"].rolling(20).mean().replace(0, 1.0)
    df_2m["rvol_2m"] = (df_2m["vol"] / vol_sma).clip(0.0, 10.0)
    rng = (df_2m["high"] - df_2m["low"]).replace(0, 1e-4)
    df_2m["body_ratio"] = ((df_2m["close"] - df_2m["open"]) / rng).clip(-1.0, 1.0)

    delta = df_2m["close"].diff()
    gain = delta.clip(lower=0.0).ewm(alpha=1/14, adjust=False).mean()
    loss = (-delta.clip(upper=0.0)).ewm(alpha=1/14, adjust=False).mean().replace(0, 1e-6)
    rs = gain / loss
    df_2m["rsi_14"] = (1.0 - (1.0 / (1.0 + rs))).clip(0.0, 1.0)

    return df_2m.dropna(subset=FEATURE_COLS)


def train_all_bots(limit: int = 25):
    t0 = time.time()
    print("==========================================================")
    print("   BOOTING 60-DAY PER-STOCK MICRO-BOT FACTORY (AZURE)     ")
    print("==========================================================")
    send_tg_event(
        stage="MICRO-BOT TRAINER (60-DAY)",
        status="START",
        details=f"Fetching 2 months of 1m candles & training {limit} stock-specific LightGBM bots..."
    )

    try:
        seed_macro_redis()

        if not os.path.exists(TARGET_TICKS_PATH):
            raise FileNotFoundError(f"{TARGET_TICKS_PATH} not found.")

        with open(TARGET_TICKS_PATH, "r") as f:
            targets = json.load(f)[:limit]

        auth = AngelAuthenticator()
        auth.generate_session()
        sc = auth.smart_connect

        end_dt = datetime.now()
        trained_count = 0
        fallback_count = 0
        total_bars = 0

        for idx, item in enumerate(targets, 1):
            token = str(item["token"])
            symbol = item.get("symbol", f"TOK_{token}")
            profile = build_sqlite_fallback_profile(token, item)

            try:
                df_1m = fetch_60d_1m_candles(sc, token, end_dt)
                if not df_1m.empty and len(df_1m) >= 300:
                    df_2m = engineer_2m_features(df_1m)
                    if len(df_2m) >= 150:
                        total_bars += len(df_2m)
                        med_price = float(df_2m["close"].iloc[-1])
                        med_atr_inr = float(df_2m["atr_14"].median())
                        med_atr_pct = float(df_2m["atr_pct"].median())
                        med_vol_2m = float(df_2m["vol"].median())

                        hurdle = max(0.0030, min(0.012, 1.4 * med_atr_pct))
                        fwd_ret = (df_2m["close"].shift(-8) - df_2m["close"]) / df_2m["close"]
                        valid_df = df_2m.iloc[:-8].copy()
                        valid_fwd = fwd_ret.iloc[:-8]

                        labels = np.select(
                            [valid_fwd < -hurdle, valid_fwd > hurdle],
                            [0, 2],
                            default=1
                        )

                        if len(np.unique(labels)) == 3:
                            X = valid_df[FEATURE_COLS].values.astype(np.float32)
                            y = labels.astype(int)
                            weights = compute_sample_weight("balanced", y=y)
                            train_ds = lgb.Dataset(X, label=y, weight=weights, feature_name=FEATURE_COLS)

                            params_lgb = {
                                "objective": "multiclass",
                                "num_class": 3,
                                "metric": "multi_logloss",
                                "boosting_type": "gbdt",
                                "learning_rate": 0.05,
                                "max_depth": 4,
                                "num_leaves": 12,
                                "min_data_in_leaf": 25,
                                "feature_fraction": 0.85,
                                "verbose": -1,
                                "n_jobs": 2,
                                "seed": 42
                            }
                            bst = lgb.train(params_lgb, train_ds, num_boost_round=100)
                            model_file = os.path.join(BOTS_DIR, f"bot_{token}.txt")
                            bst.save_model(model_file)

                            sl_pct = float(np.clip(med_atr_pct * 4.0, 0.006, 0.018))
                            profile.update({
                                "median_price": round(med_price, 2),
                                "atr_2m_inr": round(med_atr_inr, 4),
                                "atr_2m_pct": round(med_atr_pct, 5),
                                "cvd_threshold": round(float(np.clip(med_vol_2m * 0.10, 30.0, 2500.0)), 1),
                                "sl_pct": round(sl_pct, 4),
                                "tp_pct": round(sl_pct * 2.0, 4),
                                "breakeven_pct": round(sl_pct * 0.65, 4),
                                "partial_tp_pct": round(sl_pct * 1.0, 4),
                                "vwap_buffer_pct": round(float(np.clip(med_atr_pct * 0.75, 0.001, 0.0035)), 4),
                                "has_ml_model": True
                            })
                            trained_count += 1
                            print(f"  [{idx:02d}/{len(targets)}] 🤖 {symbol:<15} (#{token:<6}) | 60d 2m Bars: {len(df_2m):<5} | CVD_Thr: {profile['cvd_threshold']:<6} | SL: {profile['sl_pct']*100:.2f}%")
                        else:
                            fallback_count += 1
                            print(f"  [{idx:02d}/{len(targets)}] ⚙️ {symbol:<15} (#{token:<6}) | Statistical Profile Ready (Single-class window)")
                    else:
                        fallback_count += 1
                        print(f"  [{idx:02d}/{len(targets)}] ⚙️ {symbol:<15} (#{token:<6}) | Statistical Profile Ready (Short history)")
                else:
                    fallback_count += 1
                    print(f"  [{idx:02d}/{len(targets)}] ⚙️ {symbol:<15} (#{token:<6}) | Statistical Profile Ready (SQLite Fallback)")
            except Exception as e:
                fallback_count += 1
                print(f"  [{idx:02d}/{len(targets)}] ⚠️ {symbol:<15} (#{token:<6}) | Fallback Profile ({e})")

            profile_path = os.path.join(BOTS_DIR, f"profile_{token}.json")
            with open(profile_path, "w") as pf:
                json.dump(profile, pf, indent=4)

        elapsed = time.time() - t0
        top_names = ", ".join([t.get("symbol", "").replace("-EQ", "") for t in targets[:5]])
        send_tg_event(
            stage="MICRO-BOT TRAINER (60-DAY)",
            status="SUCCESS",
            details=(
                f"Trained: *{trained_count} ML Bots* + *{fallback_count} Stat Profiles* in `{elapsed:.1f}s`\n"
                f"📊 Total 2m Bars Processed: `{total_bars:,}`\n"
                f"🎯 Top Watchlist: `{top_names}`"
            )
        )
        print("==========================================================")
        print(f" [COMPLETE] {trained_count} ML Micro-Bots + {fallback_count} Profiles Ready in {elapsed:.1f}s!")
        print("==========================================================")

    except Exception as fatal_err:
        send_tg_event(
            stage="MICRO-BOT TRAINER (60-DAY)",
            status="ERROR",
            details=f"`{str(fatal_err)[:180]}`"
        )
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    train_all_bots()
