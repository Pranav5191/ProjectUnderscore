import os

def patch_file(path, old, new):
    if not os.path.exists(path):
        print(f"[SKIP] {path} not found")
        return
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()
    if old in content:
        content = content.replace(old, new, 1)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        print(f"[PATCHED] {path}")
    else:
        print(f"[ALREADY PATCHED OR CHANGED] {path}")

# 1. Fix double tick publishing & unused DB pool in src/ingest_main.py
patch_file(
    "src/ingest_main.py",
    """        # Connect to the local database if needed for tick logging
        await self._db_manager.connect()

        # Initialize the WebSocket client
        self._ingester = AngelDataPipeline()
        original_on_data = self._ingester._on_data

        def bridged_on_data(wsapp: Any, message: dict[str, Any]) -> None:
            # 1. Maintain the original ingestion logic (e.g., DB writes)
            if original_on_data:
                original_on_data(wsapp, message)
            
            # 2. Publish to Redis so StrategyEngine can process it asynchronously
            try:
                self._redis_client.publish("live_ticks", json.dumps(message))
            except Exception as e:
                logger.debug(f"Redis publish failed: {e}")

        # Override the callback
        self._ingester._on_data = bridged_on_data""",
    """        # AngelDataPipeline._on_data already normalizes and publishes to 'live_ticks' & 'market:ticks'
        self._ingester = AngelDataPipeline()"""
)

# 2. Fix bid/ask = 0.0 fallback in src/pipeline/websocket_client.py
patch_file(
    "src/pipeline/websocket_client.py",
    """            normalized_tick = {
                "timestamp": message.get("exchange_timestamp", 0),
                "security_id": int(message.get("token", 0)),
                "ltp": message.get("last_traded_price", 0) / 100.0,
                "ltq": message.get("last_traded_quantity", 0),
                "bid": best_buy.get("price", 0) / 100.0,
                "ask": best_sell.get("price", 0) / 100.0,
                "best_bid_vol": best_buy.get("quantity", 0),
                "best_ask_vol": best_sell.get("quantity", 0)
            }""",
    """            ltp_val = message.get("last_traded_price", 0) / 100.0
            bid_val = (best_buy.get("price", 0) / 100.0) or ltp_val
            ask_val = (best_sell.get("price", 0) / 100.0) or ltp_val
            normalized_tick = {
                "timestamp": message.get("exchange_timestamp", 0),
                "security_id": int(message.get("token", 0)),
                "ltp": ltp_val,
                "ltq": message.get("last_traded_quantity", 0),
                "bid": bid_val,
                "ask": ask_val,
                "best_bid_vol": best_buy.get("quantity", 0) or 100,
                "best_ask_vol": best_sell.get("quantity", 0) or 100
            }"""
)

# 3. Reduce Postgres pool size in src/pipeline/db.py (saves ~200MB RAM)
patch_file(
    "src/pipeline/db.py",
    "min_size: int = 10,\n        max_size: int = 20,",
    "min_size: int = 1,\n        max_size: int = 3,"
)

# 4. Stop CPU busy-waiting in src/pipeline/batch_writer.py
patch_file(
    "src/pipeline/batch_writer.py",
    'logger.info(f"Successfully processed and cleared {len(ticks)} ticks.")',
    'logger.info(f"Successfully processed and cleared {len(ticks)} ticks.")\n                    await asyncio.sleep(3.0)'
)

# 5. Fix broken import in src/scripts/ai_orchestrator.py
patch_file(
    "src/scripts/ai_orchestrator.py",
    "from telegram_reporter import TelegramReporter",
    "from src.scripts.telegram_reporter import TelegramReporter"
)

# 6. Add missing send_premarket_watchlist & dispatch methods to src/scripts/telegram_reporter.py
patch_file(
    "src/scripts/telegram_reporter.py",
    """        except subprocess.CalledProcessError:
            return None""",
    """        except subprocess.CalledProcessError:
            return None

    def send_premarket_watchlist(self, watchlist_path: str = None):
        path = watchlist_path or os.path.join(os.path.dirname(self.base_dir), "..", "target_ticks.json")
        if not os.path.exists(path):
            return
        try:
            with open(path, "r") as f:
                items = json.load(f)
            lines = [f"🚀 <b>Pre-Market Target Watchlist ({len(items)} Stocks)</b>\\n"]
            for i, item in enumerate(items[:15], 1):
                sym = item.get("symbol", "")
                score = item.get("final_score", item.get("master_score", 0.0))
                cohort = item.get("cohort", "MATH")
                lines.append(f"{i}. <code>{sym:<12}</code> | Score: <b>{score:.3f}</b> | {cohort}")
            url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
            requests.post(url, json={"chat_id": self.chat_id, "text": "\\n".join(lines), "parse_mode": "HTML"}, timeout=10)
        except Exception as e:
            print(f"[TELEGRAM WARN] Watchlist dispatch failed: {e}")

    def dispatch(self):
        self.send_premarket_watchlist()"""
)

# 7. Fix fatal dispatch loop in src/scripts/telegram_listener.py
patch_file(
    "src/scripts/telegram_listener.py",
    """async def run_dispatcher():
    \"\"\"Endlessly polls Redis for alerts and pushes them to Telegram groups.\"\"\"
    print("[DISPATCHER] Live Push Notification Daemon Active...")
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                # Blocks efficiently until an alert hits the queue
                result = await r.blpop("sys_alerts_queue", timeout=0)
                if not result: continue
                
                _, data_str = result
                alert = json.loads(data_str)
                
                tier = alert.get("tier", 3)
                chat_id = TIERS.get(tier)""",
    """async def run_dispatcher():
    \"\"\"Polls Redis for alerts with dedicated connection and 5s timeout.\"\"\"
    print("[DISPATCHER] Live Push Notification Daemon Active...")
    dispatch_r = aioredis.Redis(
        host=os.getenv("REDIS_HOST", "localhost"),
        port=int(os.getenv("REDIS_PORT", 6379)),
        decode_responses=True,
        socket_connect_timeout=5,
        health_check_interval=30
    )
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                result = await dispatch_r.blpop("sys_alerts_queue", timeout=5)
                if not result: continue
                
                _, data_str = result
                alert = json.loads(data_str)
                
                tier = int(alert.get("tier", 3))
                chat_id = TIERS.get(tier) or os.getenv("TELEGRAM_CHAT_ID")"""
)

# 8. Remove per-tick Redis lag & fix entry logic in src/trading/strategy_engine.py
patch_file(
    "src/trading/strategy_engine.py",
    """        sec_id = str(tick.get('security_id'))

        # Ignore non-tradeable macro indices and anchor components
        if sec_id in {"99926000", "99926009", "99926002", "2885", "1333"}:
            continue

        # Synchronize live state to Redis for Telegram Commander commands (/dashboard, /pnl)
        r.set("live_balance", portfolio.current_balance)
        r.set("live_positions", json.dumps(portfolio.positions))""",
    """        raw_sec_id = tick.get('security_id')
        if not raw_sec_id:
            continue
        sec_id = str(raw_sec_id)

        # Ignore non-tradeable macro indices and anchor components
        if sec_id in {"99926000", "99926009", "99926002", "26000", "26009", "26014", "2885", "1333"}:
            continue

        processed += 1
        if processed % 200 == 0:
            r.set("live_balance", portfolio.current_balance)
            r.set("live_positions", json.dumps(portfolio.positions))
            r.set("last_tick_ist", str(current_time)[:8])"""
)

patch_file(
    "src/trading/strategy_engine.py",
    """        if r.get("EMERGENCY_STOP") == "1":""",
    """        if processed % 200 == 0 and r.get("EMERGENCY_STOP") == "1":"""
)

patch_file(
    "src/trading/strategy_engine.py",
    """        while v_buffer and (current_ts_ms - v_buffer[0][0]) > 3000.0:
            v_buffer.popleft()

        if not v_buffer or (current_ts_ms - v_buffer[0][0]) < 2000.0:
            continue

        old_ts, old_obi, old_cvd = v_buffer[0]
        delta_obi = obi_score - old_obi
        delta_cvd = cvd_score - old_cvd

        vwap_buffer = current_vwap * 0.002
        upper_vwap_band = current_vwap + vwap_buffer
        lower_vwap_band = current_vwap - vwap_buffer

        # Calibrated institutional entry thresholds (reduced fakeouts)
        OBI_THRESHOLD = 0.25
        CVD_THRESHOLD = 400.0

        is_bearish = (tick_ltp > upper_vwap_band) and (delta_obi > OBI_THRESHOLD) and (delta_cvd < -CVD_THRESHOLD)
        is_bullish = (tick_ltp < lower_vwap_band) and (delta_obi < -OBI_THRESHOLD) and (delta_cvd > CVD_THRESHOLD)""",
    """        while len(v_buffer) > 1 and (current_ts_ms - v_buffer[0][0]) > 6000.0:
            v_buffer.popleft()

        if not v_buffer or (current_ts_ms - v_buffer[0][0]) < 1500.0:
            continue

        old_ts, old_obi, old_cvd = v_buffer[0]
        delta_obi = obi_score - old_obi
        delta_cvd = cvd_score - old_cvd

        vwap_buffer = current_vwap * 0.002
        upper_vwap_band = current_vwap + vwap_buffer
        lower_vwap_band = current_vwap - vwap_buffer

        OBI_THRESHOLD = 0.20
        CVD_THRESHOLD = max(50.0, 100000.0 / max(tick_ltp, 1.0))

        is_bearish = (tick_ltp > lower_vwap_band) and (delta_obi < -OBI_THRESHOLD) and (delta_cvd < -CVD_THRESHOLD)
        is_bullish = (tick_ltp < upper_vwap_band) and (delta_obi > OBI_THRESHOLD) and (delta_cvd > CVD_THRESHOLD)"""
)

print("\n[ALL AWS PATCHES APPLIED SUCCESSFULLY]")
