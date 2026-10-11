"""
src/scripts/cohort_blender.py
Blends Hybrid and Pure AI cohorts into target_ticks.json.
Guarantees a minimum stock count by backfilling from the Hybrid candidates if Pure AI yields are low.
"""

import os
import json
import logging
from typing import List, Dict, Set

logger = logging.getLogger("CohortBlender")


class CohortBlender:
    @staticmethod
    def blend_and_export(
        all_hybrid_candidates: List[Dict],
        pure_ai_candidates: List[Dict],
        target_ticks_path: str,
        hybrid_base_quota: int = 15,
        pure_ai_quota: int = 10,
        min_total_targets: int = 50
    ) -> List[Dict]:
        """
        Builds the target contract with guaranteed minimum stock count:
          1. Takes top `hybrid_base_quota` (15) from Hybrid.
          2. Takes up to `pure_ai_quota` (10) from Pure AI.
          3. If total < `min_total_targets` (50), backfills from remaining Hybrid candidates.
        """
        final_contract: List[Dict] = []
        seen_tokens: Set[str] = set()

        # Step 1: Base Hybrid Top 15
        for item in all_hybrid_candidates[:hybrid_base_quota]:
            token = str(item["token"])
            seen_tokens.add(token)
            final_contract.append({
                "symbol": item["symbol"],
                "token": token,
                "master_score": round(float(item.get("master_score", 0.0)), 4),
                "ai_score": round(float(item.get("ai_score", 0.50)), 4),
                "final_score": round(float(item.get("final_score", item.get("master_score", 0.0))), 4),
                "cohort": "HYBRID_MATH_AI"
            })

        # Step 2: Up to 10 Pure AI Catalysts
        pure_added = 0
        for item in pure_ai_candidates:
            if pure_added >= pure_ai_quota:
                break
            token = str(item["token"])
            if token in seen_tokens:
                continue
            seen_tokens.add(token)
            final_contract.append({
                "symbol": item["symbol"],
                "token": token,
                "master_score": round(float(item.get("master_score", 0.0)), 4),
                "ai_score": round(float(item.get("ai_score", 0.50)), 4),
                "final_score": round(float(item.get("final_score", item.get("ai_score", 0.50))), 4),
                "cohort": "PURE_AI_CATALYST"
            })
            pure_added += 1

        # Step 3: Backfill from remaining Hybrid candidates to hit min_total_targets (50)
        backfill_added = 0
        if len(final_contract) < min_total_targets:
            for item in all_hybrid_candidates[hybrid_base_quota:]:
                if len(final_contract) >= min_total_targets:
                    break
                token = str(item["token"])
                if token in seen_tokens:
                    continue
                seen_tokens.add(token)
                final_contract.append({
                    "symbol": item["symbol"],
                    "token": token,
                    "master_score": round(float(item.get("master_score", 0.0)), 4),
                    "ai_score": round(float(item.get("ai_score", 0.50)), 4),
                    "final_score": round(float(item.get("final_score", item.get("master_score", 0.0))), 4),
                    "cohort": "HYBRID_MATH_AI"
                })
                backfill_added += 1

        temp_path = f"{target_ticks_path}.tmp"
        with open(temp_path, "w") as f:
            json.dump(final_contract, f, indent=4)
        os.replace(temp_path, target_ticks_path)

        logger.info(
            f"Master Cohort Handoff complete: {len(final_contract)} total stocks "
            f"({hybrid_base_quota} Base Hybrid + {pure_added} Pure AI + {backfill_added} Backfilled Hybrid) "
            f"written to {target_ticks_path}"
        )
        return final_contract