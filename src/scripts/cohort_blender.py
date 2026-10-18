"""
src/scripts/cohort_blender.py
Blends Hybrid and Pure AI cohorts into target_ticks.json while preserving full AI directional metadata.
"""
import os
import json
import logging
from typing import List, Dict, Set

logger = logging.getLogger("CohortBlender")


class CohortBlender:
    @staticmethod
    def _format_entry(item: Dict, cohort_name: str) -> Dict:
        return {
            "symbol": item["symbol"],
            "token": str(item["token"]),
            "master_score": round(float(item.get("master_score", 0.0)), 4),
            "ai_score": round(float(item.get("ai_score", 0.50)), 4),
            "final_score": round(float(item.get("final_score", item.get("master_score", 0.0))), 4),
            "catalyst_intensity": round(float(item.get("catalyst_intensity", 0.0)), 4),
            "sentiment_bias": str(item.get("sentiment_bias", "NEUTRAL")).upper(),
            "direction_bias": str(item.get("direction_bias", "NEUTRAL")).upper(),
            "reasoning": str(item.get("reasoning", ""))[:280],
            "cohort": cohort_name
        }

    @staticmethod
    def blend_and_export(
        all_hybrid_candidates: List[Dict],
        pure_ai_candidates: List[Dict],
        target_ticks_path: str,
        hybrid_base_quota: int = 20,
        pure_ai_quota: int = 5,
        min_total_targets: int = 25
    ) -> List[Dict]:
        final_contract: List[Dict] = []
        seen_tokens: Set[str] = set()

        for item in all_hybrid_candidates[:hybrid_base_quota]:
            token = str(item["token"])
            seen_tokens.add(token)
            final_contract.append(CohortBlender._format_entry(item, "HYBRID_MATH_AI"))

        pure_added = 0
        for item in pure_ai_candidates:
            if pure_added >= pure_ai_quota:
                break
            token = str(item["token"])
            if token in seen_tokens:
                continue
            seen_tokens.add(token)
            final_contract.append(CohortBlender._format_entry(item, "PURE_AI_CATALYST"))
            pure_added += 1

        backfill_added = 0
        if len(final_contract) < min_total_targets:
            for item in all_hybrid_candidates[hybrid_base_quota:]:
                if len(final_contract) >= min_total_targets:
                    break
                token = str(item["token"])
                if token in seen_tokens:
                    continue
                seen_tokens.add(token)
                final_contract.append(CohortBlender._format_entry(item, "HYBRID_MATH_AI"))
                backfill_added += 1

        temp_path = f"{target_ticks_path}.tmp"
        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(final_contract, f, indent=4)
        os.replace(temp_path, target_ticks_path)

        logger.info(
            f"Master Cohort Handoff complete: {len(final_contract)} total stocks "
            f"({hybrid_base_quota} Base Hybrid + {pure_added} Pure AI + {backfill_added} Backfilled Hybrid) "
            f"written to {target_ticks_path}"
        )
        return final_contract
