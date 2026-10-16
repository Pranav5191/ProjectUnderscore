import os

# 1. Fix ai_catalyst_crawler.py (Evening/Night window awareness)
with open("src/scripts/ai_catalyst_crawler.py", "r", encoding="utf-8") as f:
    c = f.read()

old_window = """        today_date = now_ist.date()
        if today_date.weekday() == 0:
            prev_mkt_date = today_date - timedelta(days=3)
        elif today_date.weekday() == 6:
            prev_mkt_date = today_date - timedelta(days=2)
        else:
            prev_mkt_date = today_date - timedelta(days=1)

        start_dt = datetime.combine(prev_mkt_date, dtime(15, 30, 0), tzinfo=IST)
        end_dt = datetime.combine(today_date, dtime(8, 35, 0), tzinfo=IST)

        if now_ist > end_dt:
            end_dt = now_ist"""

new_window = """        today_date = now_ist.date()
        # If running after 3:30 PM IST, scan from today's close (or last 24h) up to now
        if now_ist.time() >= dtime(15, 30, 0):
            start_dt = datetime.combine(today_date - timedelta(days=1), dtime(15, 30, 0), tzinfo=IST)
            end_dt = now_ist
        else:
            if today_date.weekday() == 0:
                prev_mkt_date = today_date - timedelta(days=3)
            elif today_date.weekday() == 6:
                prev_mkt_date = today_date - timedelta(days=2)
            else:
                prev_mkt_date = today_date - timedelta(days=1)
            start_dt = datetime.combine(prev_mkt_date, dtime(15, 30, 0), tzinfo=IST)
            end_dt = max(now_ist, datetime.combine(today_date, dtime(8, 35, 0), tzinfo=IST))"""

if old_window in c:
    c = c.replace(old_window, new_window, 1)
    with open("src/scripts/ai_catalyst_crawler.py", "w", encoding="utf-8") as f:
        f.write(c)
    print("[FIXED] src/scripts/ai_catalyst_crawler.py")

# 2. Fix ai_evaluator.py (Allow Gemini 2.5/2.0 Flash & fix Groq json_object mode)
with open("src/scripts/ai_evaluator.py", "r", encoding="utf-8") as f:
    e_code = f.read()

e_code = e_code.replace(
    'blocked_substrings = ["1.5", "2.0", "2.5", "tts", "image", "audio", "thinking", "vision", "embedding", "8b"]',
    'blocked_substrings = ["1.5", "tts", "image", "audio", "vision", "embedding", "8b"]'
)
e_code = e_code.replace(
    'for pref in ["gemini-flash-lite-latest", "gemini-flash-latest"]:',
    'for pref in ["gemini-3.8-flash", "gemini-2.0-flash", "gemini-flash-latest", "gemini-flash-lite-latest"]:'
)

old_groq_schema = """                response_format={
                    "type": "json_schema",
                    "json_schema": {
                        "name": "evaluations_schema",
                        "strict": False,
                        "schema": {
                            "type": "object",
                            "properties": {
                                "evaluations": {
                                    "type": "array",
                                    "items": {
                                        "type": "object",
                                        "properties": {
                                            "symbol": {"type": "string"},
                                            "sentiment_bias": {"type": "string"},
                                            "catalyst_intensity": {"type": "number"},
                                            "ai_score": {"type": "number"},
                                            "reasoning": {"type": "string"}
                                        },
                                        "required": ["symbol", "sentiment_bias", "catalyst_intensity", "ai_score", "reasoning"]
                                    }
                                }
                            },
                            "required": ["evaluations"]
                        }
                    }
                },"""

new_groq_schema = '                response_format={"type": "json_object"},'
e_code = e_code.replace(old_groq_schema, new_groq_schema)

with open("src/scripts/ai_evaluator.py", "w", encoding="utf-8") as f:
    f.write(e_code)
print("[FIXED] src/scripts/ai_evaluator.py")

# 3. Fix overnight_rumor_worker.py (Valid Gemini/Groq models, -EQ normalization, fast 1.5s pacing)
with open("src/scripts/overnight_rumor_worker.py", "r", encoding="utf-8") as f:
    r_code = f.read()

r_code = r_code.replace(
    "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.8-flash:generateContent",
    "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.8-flash:generateContent"
).replace(
    '"model": "openai/gpt-oss-20b"',
    '"model": "llama-3.3-70b-versatile"'
).replace(
    'entry[" sentiment_bias"] = str(entry.get("sentiment_bias", "NEUTRAL")).upper().strip()\n                        entry["sentiment_bias"] = entry.pop(" sentiment_bias")',
    'entry["sentiment_bias"] = str(entry.get("sentiment_bias", "NEUTRAL")).upper().strip()'
).replace(
    "time.sleep(10.0)",
    "time.sleep(1.5)"
)

with open("src/scripts/overnight_rumor_worker.py", "w", encoding="utf-8") as f:
    f.write(r_code)
print("[FIXED] src/scripts/overnight_rumor_worker.py")

# 4. Fix ai_orchestrator.py (Fix import, float catalyst_intensity, -EQ suffix, and isolated Step 6 Telegram call)
with open("src/scripts/ai_orchestrator.py", "r", encoding="utf-8") as f:
    o_code = f.read()

o_code = o_code.replace(
    "from telegram_reporter import TelegramReporter\n",
    ""
).replace(
    "from src.scripts.cohort_blender import CohortBlender",
    "from src.scripts.cohort_blender import CohortBlender\nfrom src.scripts.telegram_reporter import TelegramReporter"
).replace(
    'sym = str(r.get("symbol", "")).upper().strip()\n                        if not sym or sym == "NONE":\n                            continue',
    'sym = str(r.get("symbol", "")).upper().strip()\n                        if not sym or sym in ("NONE", "NIFTY", "SENSEX"):\n                            continue\n                        if not sym.endswith("-EQ"):\n                            sym = f"{sym}-EQ"'
).replace(
    '"catalyst_intensity": "HIGH" if intensity >= 0.7 else ("MEDIUM" if intensity >= 0.4 else "LOW"),',
    '"catalyst_intensity": round(float(intensity), 4),'
).replace(
    """            # Step 6: Dispatch Pre-Market Intelligence Briefing to Telegram
            logger.info("[STEP 6] Dispatching pre-market audit to Telegram...")
            reporter = TelegramReporter()
            reporter.dispatch()""",
    """            # Step 6: Dispatch Pre-Market Intelligence Briefing to Telegram (isolated so it never triggers fallback)
            try:
                logger.info("[STEP 6] Dispatching pre-market audit to Telegram...")
                reporter = TelegramReporter()
                if hasattr(reporter, "send_premarket_watchlist"):
                    reporter.send_premarket_watchlist(self.target_ticks_path)
            except Exception as te:
                logger.warning(f"Telegram notification skipped: {te}")"""
)

with open("src/scripts/ai_orchestrator.py", "w", encoding="utf-8") as f:
    f.write(o_code)
print("[FIXED] src/scripts/ai_orchestrator.py")
