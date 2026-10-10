"""
src/scripts/ai_evaluator.py
Dual-Engine Batched Structured LLM Catalyst Evaluator.
Primary: Google Gemini Flash-Lite/Flash
Failover: Groq Llama-3.3-70B-Versatile (JSON Schema Mode)
"""

import os
import json
import time
import logging
import requests
from typing import Dict, List, Any, Optional

try:
    from groq import Groq
except ImportError:
    Groq = None

logger = logging.getLogger("AIEvaluator")

NEUTRAL_FALLBACK_RESULT = {
    "sentiment_bias": "NEUTRAL",
    "catalyst_intensity": 0.0,
    "ai_score": 0.50,
    "reasoning": "No material overnight catalyst or fallback neutral score applied."
}

BATCH_SYSTEM_PROMPT = """You are an institutional Indian equity (NSE) quantitative event-driven analyst.
You will be given overnight corporate filings, HFT-filtered institutional bulk/block deals, insider PIT disclosures, and scheduled board meetings for a batch of NSE stock tickers.
Evaluate EVERY ticker provided and return ONLY a valid JSON object matching this exact schema:
{
  "evaluations": [
    {
      "symbol": "<exact ticker symbol from input>",
      "sentiment_bias": "BULLISH" | "BEARISH" | "NEUTRAL",
      "catalyst_intensity": <float between 0.0 and 1.0>,
      "ai_score": <float between 0.0 and 1.0>,
      "reasoning": "<concise 1-sentence catalyst summary>"
    }
  ]
}

Scoring Rules:
- Scale order wins / deal values relative to the stock's price & average daily turnover when provided.
- catalyst_intensity: 0.0 = routine administrative notice; 0.35-0.60 = moderate event; 0.75-1.0 = massive material event (large order win, strong earnings beat, net institutional accumulation, promoter buying, demerger, regulatory action, CXO/auditor resignation).
- ai_score: 0.50 is strictly neutral. >0.70 is high-conviction intraday bullish catalyst. <0.30 is high-conviction bearish catalyst/distribution.
- Keep reasoning to 1 crisp sentence."""


class CatalystEvaluator:
    def __init__(
        self,
        batch_size: int = 8,
        request_timeout: float = 30.0
    ):
        self.gemini_api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
        self.groq_api_key = os.getenv("GROQ_API_KEY")
        self.batch_size = batch_size
        self.timeout = request_timeout

        self.gemini_fallback_models: List[str] = []
        self._build_gemini_chain()

        self.groq_client = None
        if self.groq_api_key and Groq:
            try:
                self.groq_client = Groq(api_key=self.groq_api_key)
                logger.info("Initialized Groq client for Llama 3.3 70B failover.")
            except Exception as e:
                logger.warning(f"Failed to initialize Groq client: {e}")

    def _build_gemini_chain(self):
        """Builds a strict list of active Gemini models, explicitly excluding retired names."""
        if not self.gemini_api_key:
            return
            
        blocked_substrings = ["1.5", "2.0", "2.5", "tts", "image", "audio", "thinking", "vision", "embedding", "8b"]
        try:
            url = f"https://generativelanguage.googleapis.com/v1beta/models?key={self.gemini_api_key}"
            resp = requests.get(url, timeout=10)
            if resp.status_code == 200:
                models = resp.json().get("models", [])
                available = [
                    m["name"].replace("models/", "")
                    for m in models
                    if "generateContent" in m.get("supportedGenerationMethods", [])
                ]
                active_candidates = [
                    m for m in available
                    if ("flash" in m.lower() or "pro" in m.lower())
                    and not any(b in m.lower() for b in blocked_substrings)
                ]

                chain = []
                for pref in ["gemini-flash-lite-latest", "gemini-flash-latest"]:
                    if pref in available and pref not in chain:
                        chain.append(pref)

                for cand in active_candidates:
                    if cand not in chain:
                        chain.append(cand)

                self.gemini_fallback_models = chain if chain else ["gemini-flash-lite-latest", "gemini-flash-latest"]
                logger.info(f"Initialized clean Gemini failover chain: {self.gemini_fallback_models}")
            else:
                self.gemini_fallback_models = ["gemini-flash-lite-latest", "gemini-flash-latest"]
        except Exception as e:
            logger.warning(f"Model chain discovery skipped ({e}); using default failover.")
            self.gemini_fallback_models = ["gemini-flash-lite-latest", "gemini-flash-latest"]

    @staticmethod
    def validate_and_sanitize(raw_obj: Dict[str, Any]) -> Dict[str, Any]:
        bias = str(raw_obj.get("sentiment_bias", "NEUTRAL")).upper().strip()
        if bias not in {"BULLISH", "BEARISH", "NEUTRAL"}:
            bias = "NEUTRAL"

        try:
            intensity = max(0.0, min(1.0, round(float(raw_obj.get("catalyst_intensity", 0.0)), 4)))
        except (TypeError, ValueError):
            intensity = 0.0

        try:
            ai_score = max(0.0, min(1.0, round(float(raw_obj.get("ai_score", 0.50)), 4)))
        except (TypeError, ValueError):
            ai_score = 0.50

        reasoning = str(raw_obj.get("reasoning", "")).strip() or "Evaluated overnight corporate filing."
        return {
            "sentiment_bias": bias,
            "catalyst_intensity": intensity,
            "ai_score": ai_score,
            "reasoning": reasoning[:280]
        }

    def _call_groq_fallback(self, user_prompt: str) -> Optional[List[Dict]]:
        """Calls Groq's Llama 3.3 70B Versatile model using JSON Schema Output."""
        if not self.groq_client:
            return None
            
        try:
            logger.info("Executing GROQ Llama 3.3 70B Fallback...")
            response = self.groq_client.chat.completions.create(
                model="llama-3.3-70b-versatile",
                messages=[
                    {"role": "system", "content": BATCH_SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt}
                ],
                response_format={
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
                },
                temperature=0.1,
                max_tokens=1500
            )
            
            content = response.choices[0].message.content
            parsed = json.loads(content)
            return parsed.get("evaluations", [])
            
        except Exception as e:
            logger.error(f"Groq fallback failed: {e}")
            return None

    def _evaluate_chunk(
        self,
        chunk_map: Dict[str, List[Dict]],
        stock_context_map: Optional[Dict[str, str]] = None
    ) -> Dict[str, Dict[str, Any]]:
        blocks = []
        for sym, filings in chunk_map.items():
            ctx_str = f" ({stock_context_map[sym]})" if (stock_context_map and sym in stock_context_map) else ""
            filing_lines = "\n".join(
                f"  * [{f.get('source', 'FILING')} | {f.get('timestamp', '')}] {f.get('subject', '')}: {f.get('details', '')[:450]}"
                for f in filings[:5]
            )
            blocks.append(f"=== SYMBOL: {sym}{ctx_str} ===\n{filing_lines}")

        user_prompt = "Evaluate the following NSE tickers and their overnight events:\n\n" + "\n\n".join(blocks)

        # 1. Primary Engine: Gemini Chain
        payload = {
            "systemInstruction": {"parts": [{"text": BATCH_SYSTEM_PROMPT}]},
            "contents": [{"parts": [{"text": user_prompt}]}],
            "generationConfig": {
                "temperature": 0.1,
                "responseMimeType": "application/json",
                "responseSchema": {
                    "type": "OBJECT",
                    "properties": {
                        "evaluations": {
                            "type": "ARRAY",
                            "items": {
                                "type": "OBJECT",
                                "properties": {
                                    "symbol": {"type": "STRING"},
                                    "sentiment_bias": {"type": "STRING"},
                                    "catalyst_intensity": {"type": "NUMBER"},
                                    "ai_score": {"type": "NUMBER"},
                                    "reasoning": {"type": "STRING"}
                                },
                                "required": ["symbol", "sentiment_bias", "catalyst_intensity", "ai_score", "reasoning"]
                            }
                        }
                    },
                    "required": ["evaluations"]
                }
            }
        }

        attempt_sequence = [self.gemini_fallback_models[0]] + self.gemini_fallback_models if self.gemini_fallback_models else []
        last_error = None
        parsed_list = None
        active_model = None

        if self.gemini_api_key:
            for idx, model_candidate in enumerate(attempt_sequence):
                url = (
                    f"https://generativelanguage.googleapis.com/v1beta/models/"
                    f"{model_candidate}:generateContent?key={self.gemini_api_key}"
                )
                try:
                    resp = requests.post(url, json=payload, timeout=self.timeout)
                    if resp.status_code == 200:
                        data = resp.json()
                        text_out = data["candidates"][0]["content"]["parts"][0]["text"]
                        parsed_list = json.loads(text_out).get("evaluations", [])
                        active_model = model_candidate
                        break

                    elif resp.status_code in (429, 503):
                        wait_sec = 2.0 * (idx + 1)
                        logger.warning(f"[RETRY] Batch hit HTTP {resp.status_code} on {model_candidate}. Retrying in {wait_sec:.0f}s...")
                        last_error = f"HTTP {resp.status_code} on {model_candidate}"
                        time.sleep(wait_sec)
                    else:
                        last_error = f"HTTP {resp.status_code}: {resp.text[:120]}"
                except Exception as e:
                    last_error = str(e)
                    time.sleep(1.5)

        # 2. Secondary Engine: Groq Llama 3.3 70B Failover
        if parsed_list is None:
            logger.warning(f"[FAILOVER] Gemini chain exhausted ({last_error}). Routing to Groq Llama 3.3...")
            parsed_list = self._call_groq_fallback(user_prompt)
            active_model = "groq-llama-3.3-70b"

        # Process Results
        chunk_results: Dict[str, Dict[str, Any]] = {}
        if isinstance(parsed_list, list):
            for item in parsed_list:
                sym_key = str(item.get("symbol", "")).strip().upper()
                if not sym_key.endswith("-EQ") and f"{sym_key}-EQ" in chunk_map:
                    sym_key = f"{sym_key}-EQ"
                if sym_key in chunk_map:
                    clean_eval = self.validate_and_sanitize(item)
                    chunk_results[sym_key] = clean_eval
                    logger.info(
                        f"[AI SCORED] {sym_key:<15} | Bias: {clean_eval['sentiment_bias']:<7} "
                        f"| AI Score: {clean_eval['ai_score']:.2f} "
                        f"| Intensity: {clean_eval['catalyst_intensity']:.2f} "
                        f"| Model: {active_model}"
                    )

        # 0.50 Fallback for missing symbols
        for sym in chunk_map:
            if sym not in chunk_results:
                chunk_results[sym] = dict(NEUTRAL_FALLBACK_RESULT)

        return chunk_results

    def evaluate_batch(
        self,
        symbol_filings_map: Dict[str, List[Dict]],
        stock_context_map: Optional[Dict[str, str]] = None
    ) -> Dict[str, Dict[str, Any]]:
        results: Dict[str, Dict[str, Any]] = {}
        active_items = [(sym, flist) for sym, flist in symbol_filings_map.items() if flist]

        if not active_items:
            return results

        if not self.gemini_api_key and not self.groq_api_key:
            logger.warning("No API keys found! Assigning 0.50 neutral scores.")
            return {sym: dict(NEUTRAL_FALLBACK_RESULT) for sym, _ in active_items}

        for i in range(0, len(active_items), self.batch_size):
            chunk = dict(active_items[i:i + self.batch_size])
            chunk_res = self._evaluate_chunk(chunk, stock_context_map=stock_context_map)
            results.update(chunk_res)
            if i + self.batch_size < len(active_items):
                time.sleep(1.0)

        return results