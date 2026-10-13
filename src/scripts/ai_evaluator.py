import os
import json
import time
import requests
import logging
from typing import Dict, List, Any, Optional
import re

try:
    from groq import Groq
except ImportError:
    Groq = None

logger = logging.getLogger("AIEvaluator")

BATCH_SYSTEM_PROMPT = """You are a financial AI evaluating overnight NSE corporate filings.
Analyze the events and output a JSON array of evaluations.
Score ai_score between 0.0 (bearish/irrelevant) and 1.0 (highly bullish/impactful)."""

NEUTRAL_FALLBACK_RESULT = {
    "sentiment_bias": "NEUTRAL",
    "catalyst_intensity": 0.0,
    "ai_score": 0.50,
    "reasoning": "Fallback applied due to API constraints."
}

class CatalystEvaluator:
    def __init__(
        self,
        batch_size: int = 8,
        request_timeout: float = 8.0
    ):
        self.gemini_api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
        self.groq_api_key = os.getenv("GROQ_API_KEY")
        self.batch_size = batch_size
        self.timeout = request_timeout
        
        # STICKY CIRCUIT BREAKER
        self.gemini_exhausted = False

        self.gemini_fallback_models: List[str] = []
        self._build_gemini_chain()

        self.groq_client = None
        if self.groq_api_key and Groq:
            try:
                self.groq_client = Groq(api_key=self.groq_api_key)
                logger.info("Initialized Groq client for Llama failover.")
            except Exception as e:
                logger.warning(f"Failed to initialize Groq client: {e}")

    def _build_gemini_chain(self):
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
                
                self.gemini_fallback_models = chain
        except Exception as e:
            logger.warning(f"Failed to build Gemini chain: {e}")

    def validate_and_sanitize(self, raw_obj: Dict[str, Any]) -> Dict[str, Any]:
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

    def _call_groq_fallback(self, user_prompt: str) -> list:
        if not getattr(self, "groq_client", None):
            return None

        try:
            import json
            import re
            
            logger.info(f"Executing GROQ Fallback (Batch of {getattr(self, 'batch_size', 8)}) on qwen/qwen3.8-27b...")
            
            # Using your rubric but explicitly asking for JSON without forcing the API mode
            system_msg = BATCH_SYSTEM_PROMPT + "\n\nCRITICAL INSTRUCTION: You must output ONLY valid JSON. The output must be an object with a single key 'evaluations' containing the array of results. Do not include markdown formatting or explanation text."
            
            response = self.groq_client.chat.completions.create(
                model="qwen/qwen3.8-27b",
                messages=[
                    {"role": "system", "content": system_msg},
                    {"role": "user", "content": user_prompt}
                ]
            )
            raw_text = response.choices[0].message.content
            
            # Smart extraction to handle if the model wraps it in markdown anyway
            match = re.search(r'\{.*\}', raw_text, re.DOTALL)
            if match:
                raw_text = match.group(0)
                
            return json.loads(raw_text).get("evaluations", [])
            
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
            lines = []
            for f in filings[:5]:
                src = f.get('source', 'FILING')
                ts = f.get('timestamp', '')
                subj = f.get('subject', '')
                details = str(f.get('details', ''))[:450]
                lines.append(f"  * [{src} | {ts}] {subj}: {details}")
            filing_lines = "\n".join(lines)
            blocks.append(f"=== SYMBOL: {sym}{ctx_str} ===\n{filing_lines}")

        user_prompt = "Evaluate the following NSE tickers and their overnight events:\n\n" + "\n\n".join(blocks)
        parsed_list = None
        active_model = None

        # TIER 1: GEMINI
        if self.gemini_api_key and not self.gemini_exhausted:
            payload = {
                "systemInstruction": {"parts": [{"text": BATCH_SYSTEM_PROMPT}]},
                "contents": [{"parts": [{"text": user_prompt}]}],
                "generationConfig": {"temperature": 0.1, "responseMimeType": "application/json"}
            }
            for model_candidate in list(dict.fromkeys(self.gemini_fallback_models)):
                url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_candidate}:generateContent?key={self.gemini_api_key}"
                try:
                    logger.info(f"[TIER 1] Attempting Gemini model: {model_candidate}...")
                    resp = requests.post(url, json=payload, timeout=30.0)
                    if resp.status_code == 200:
                        data = resp.json()
                        text_out = data["candidates"][0]["content"]["parts"][0]["text"]
                        parsed_list = json.loads(text_out).get("evaluations", [])
                        active_model = f"gemini-{model_candidate}"
                        break
                    elif resp.status_code in (429, 503, 500, 502):
                        self.gemini_exhausted = True
                        break
                except Exception:
                    self.gemini_exhausted = True
                    break

        # TIER 2: GROQ LLAMA
        if parsed_list is None and self.groq_client:
            for groq_model in ["llama-3.3-70b-versatile", "llama-3.1-8b-instant"]:
                try:
                    logger.info(f"[TIER 2] Attempting Groq model: {groq_model}...")
                    system_msg = BATCH_SYSTEM_PROMPT + "\n\nCRITICAL: Return ONLY a valid JSON object. Top-level key must be 'evaluations'."
                    response = self.groq_client.chat.completions.create(
                        model=groq_model,
                        messages=[{"role": "system", "content": system_msg}, {"role": "user", "content": user_prompt}],
                        response_format={"type": "json_object"},
                        timeout=30.0
                    )
                    raw_text = response.choices[0].message.content
                    match = re.search(r'\{.*\}', raw_text, re.DOTALL)
                    if match: raw_text = match.group(0)
                    parsed_list = json.loads(raw_text).get("evaluations", [])
                    active_model = f"groq-{groq_model}"
                    break
                except Exception as e:
                    logger.warning(f"[TIER 2] Groq model {groq_model} failed: {e}")

        # TIER 3: QWEN MODEL
        if parsed_list is None and self.groq_client:
            try:
                logger.info("[TIER 3] Attempting Qwen model: qwen/qwen3.8-27b...")
                system_msg = BATCH_SYSTEM_PROMPT + "\n\nCRITICAL: Return ONLY a valid JSON object. Top-level key must be 'evaluations'."
                response = self.groq_client.chat.completions.create(
                    model="qwen/qwen3.8-27b",
                    messages=[{"role": "system", "content": system_msg}, {"role": "user", "content": user_prompt}],
                    response_format={"type": "json_object"},
                    timeout=30.0
                )
                raw_text = response.choices[0].message.content
                match = re.search(r'\{.*\}', raw_text, re.DOTALL)
                if match: raw_text = match.group(0)
                parsed_list = json.loads(raw_text).get("evaluations", [])
                active_model = "qwen-qwen3.8-27b"
            except Exception as e:
                logger.warning(f"[TIER 3] Qwen failed: {e}")

        # TIER 4: OPENAI OSS
        if parsed_list is None and self.groq_client:
            try:
                logger.info("[TIER 4] Attempting OpenAI OSS model: openai/gpt-oss-20b...")
                system_msg = BATCH_SYSTEM_PROMPT + "\n\nCRITICAL: Return ONLY a valid JSON object. Top-level key must be 'evaluations'."
                response = self.groq_client.chat.completions.create(
                    model="openai/gpt-oss-20b",
                    messages=[{"role": "system", "content": system_msg}, {"role": "user", "content": user_prompt}],
                    response_format={"type": "json_object"},
                    timeout=30.0
                )
                raw_text = response.choices[0].message.content
                match = re.search(r'\{.*\}', raw_text, re.DOTALL)
                if match: raw_text = match.group(0)
                parsed_list = json.loads(raw_text).get("evaluations", [])
                active_model = "openai-gpt-oss-20b"
            except Exception as e:
                logger.warning(f"[TIER 4] OpenAI OSS failed: {e}")

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
                        f"| AI Score: {clean_eval['ai_score']:.2f} | Model: {active_model}"
                    )

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
            
            # API Rate-Limit Protection for Batches
            if i + self.batch_size < len(active_items):
                time.sleep(1.0) 

        return results
