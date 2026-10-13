"""
overnight_rumor_worker.py
Midnight Shift Harvester (High-Capacity Edition)
Scrapes 50-100+ market articles across major Indian financial feeds,
normalizes exact NSE symbols, scores catalyst intensity via Gemini/Groq,
and exports sorted rumors to data/overnight_rumors.json.
"""

import os
import json
import time
import logging
import feedparser
from typing import List, Dict, Optional
from dotenv import load_dotenv

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(name)-15s | %(message)s"
)
logger = logging.getLogger("MidnightHarvester")

load_dotenv()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")

OUTPUT_PATH = "data/overnight_rumors.json"

class OvernightHarvester:
    def __init__(self):
        # Multi-source RSS network for broad Indian market coverage
        self.rss_urls = [
            "https://economictimes.indiatimes.com/markets/rssfeeds/2146842.cms",          # ET Markets
            "https://economictimes.indiatimes.com/markets/stocks/rssfeeds/2146843.cms",   # ET Stocks
            "https://www.moneycontrol.com/rss/marketreports.xml",                         # Moneycontrol Markets
            "https://www.moneycontrol.com/rss/business.xml",                              # Moneycontrol Business
            "https://www.livemint.com/rss/markets",                                       # LiveMint Markets
            "https://www.business-standard.com/rss/markets-106.rss",                      # Business Standard
            "https://www.financialexpress.com/market/feed/"                               # Financial Express
        ]
        self.rumors = []

    def scrape_all_feeds(self, target_min: int = 50, target_max: int = 120):
        logger.info(f"Initiating high-capacity RSS scrape across {len(self.rss_urls)} channels...")
        seen_titles = set()

        for url in self.rss_urls:
            try:
                feed = feedparser.parse(url)
                # Pull up to 25 items per feed
                for entry in feed.entries[:25]:
                    title_clean = entry.title.strip()
                    title_key = title_clean.lower()[:60]  # Deduplicate cross-published headlines
                    
                    if title_key not in seen_titles:
                        seen_titles.add(title_key)
                        self.rumors.append({
                            "source": "RSS",
                            "title": title_clean,
                            "summary": entry.get("summary", "")[:600],
                            "url": entry.get("link", "")
                        })
                    if len(self.rumors) >= target_max:
                        break
            except Exception as e:
                logger.warning(f"Failed to fetch feed {url}: {e}")

            if len(self.rumors) >= target_max:
                break

        logger.info(f"Harvested {len(self.rumors)} unique financial articles (Target: {target_min}-{target_max}).")

class RumorEvaluator:
    def __init__(self):
        self.gemini_url = "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.8-flash:generateContent"
        self.groq_url = "https://api.groq.com/openai/v1/chat/completions"
        self.gemini_exhausted = False

    def _call_groq(self, prompt: str) -> Optional[Dict]:
        if not GROQ_API_KEY:
            logger.error("GROQ_API_KEY missing.")
            return None
        try:
            import httpx
            headers = {
                "Authorization": f"Bearer {GROQ_API_KEY}",
                "Content-Type": "application/json"
            }
            payload = {
                "model": "llama-3.1-8b-instant",
                "messages": [
                    {
                        "role": "system", 
                        "content": "You are a financial catalyst engine for the National Stock Exchange of India (NSE). Always output valid JSON only with keys: symbol, sentiment_bias, rumor_intensity, reasoning."
                    },
                    {"role": "user", "content": prompt}
                ],
                "temperature": 0.1,
                "response_format": {"type": "json_object"}
            }
            res = httpx.post(self.groq_url, headers=headers, json=payload, timeout=12.0)
            if res.status_code == 200:
                raw_json = res.json()["choices"][0]["message"]["content"].strip()
                return json.loads(raw_json)
        except Exception as e:
            logger.warning(f"Groq call exception: {e}")
        return None

    def _call_gemini(self, prompt: str) -> Optional[Dict]:
        try:
            import httpx
            headers = {
                "x-goog-api-key": GEMINI_API_KEY,
                "Content-Type": "application/json"
            }
            payload = {"contents": [{"parts": [{"text": prompt}]}]}
            res = httpx.post(self.gemini_url, headers=headers, json=payload, timeout=12.0)
            if res.status_code == 200:
                raw = res.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
                if raw.startswith("```"):
                    raw = raw.strip("`").replace("json\n", "", 1).strip()
                return json.loads(raw)
            elif res.status_code in (429, 503, 500, 502):
                logger.warning(f"[CIRCUIT BREAKER] Gemini {res.status_code} encountered. Switching to Groq for remainder.")
                self.gemini_exhausted = True
                return None
            else:
                return None
        except Exception as e:
            logger.warning(f"[CIRCUIT BREAKER] Gemini connection error ({e}). Switching to Groq.")
            self.gemini_exhausted = True
            return None

    def evaluate_and_stream(self, raw_data: List[Dict]):
        os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
        results = []
        if os.path.exists(OUTPUT_PATH):
            try:
                with open(OUTPUT_PATH, "r") as f:
                    results = json.load(f)
            except Exception:
                results = []

        seen_symbols = {r.get("symbol") for r in results if r.get("symbol")}

        total_items = len(raw_data)
        logger.info(f"Evaluating {total_items} items with live incremental ranking...")

        for idx, item in enumerate(raw_data, 1):
            prompt = f"""Analyze this Indian financial headline and extract company-specific catalysts:
Title: {item['title']}
Body: {item['summary']}

Requirements:
1. "symbol": Exact NSE trading ticker symbol (e.g. RELIANCE, TATAMOTORS, ALOKINDS, HDFCBANK). If broad indices (NIFTY/SENSEX), US stocks, or no specific listed Indian stock, output "NONE".
2. "sentiment_bias": "BULLISH", "BEARISH", or "NEUTRAL".
3. "rumor_intensity": Float from 0.0 to 1.0 (0.9 = block deal / leak / massive contract, 0.5 = quarterly earnings, 0.1 = broad commentary).
4. "reasoning": 1 concise sentence explaining the catalyst.

JSON format only:"""

            parsed = None
            if not self.gemini_exhausted:
                parsed = self._call_gemini(prompt)

            if not parsed:
                parsed = self._call_groq(prompt)

            # Normalize single dict, list of dicts, or nested wrapper dict into a flat list
            candidates = []
            if isinstance(parsed, list):
                candidates = [x for x in parsed if isinstance(x, dict)]
            elif isinstance(parsed, dict):
                if "symbol" in parsed:
                    candidates = [parsed]
                else:
                    for val in parsed.values():
                        if isinstance(val, list):
                            candidates.extend([x for x in val if isinstance(x, dict)])

            extracted_any = False
            for entry in candidates:
                try:
                    raw_sym = entry.get("symbol")
                    if not raw_sym or not isinstance(raw_sym, str):
                        continue
                    sym = raw_sym.upper().strip().replace(" ", "")
                    if sym in ("NONE", "", "NULL", "N/A", "NIFTY", "SENSEX"):
                        continue
                    if sym == "ALOK":
                        sym = "ALOKINDS"

                    extracted_any = True
                    if sym not in seen_symbols:
                        seen_symbols.add(sym)
                        entry["symbol"] = sym
                        entry[" sentiment_bias"] = str(entry.get("sentiment_bias", "NEUTRAL")).upper().strip()
                        entry["sentiment_bias"] = entry.pop(" sentiment_bias")
                        entry["rumor_intensity"] = float(entry.get("rumor_intensity", 0.5))
                        entry["reasoning"] = str(entry.get("reasoning", "Overnight news catalyst.")).strip()
                        results.append(entry)

                        results.sort(key=lambda x: x.get("rumor_intensity", 0.0), reverse=True)
                        with open(OUTPUT_PATH, "w") as f:
                            json.dump(results, f, indent=4)
                        logger.info(f"[{idx}/{total_items}] Added {sym} (Intensity: {entry['rumor_intensity']}) -> {OUTPUT_PATH}")
                    else:
                        logger.info(f"[{idx}/{total_items}] Duplicate ticker {sym} skipped.")
                except Exception as item_err:
                    logger.warning(f"[{idx}/{total_items}] Skipped malformed item: {item_err}")

            if not extracted_any:
                logger.info(f"[{idx}/{total_items}] No company-specific ticker identified.")

            time.sleep(10.0)

if __name__ == "__main__":
    harvester = OvernightHarvester()
    harvester.scrape_all_feeds(target_min=50, target_max=100)

    evaluator = RumorEvaluator()
    evaluator.evaluate_and_stream(harvester.rumors)
    logger.info(f"Overnight Shift Complete. Processed {len(harvester.rumors)} headlines.")
