# src/scoring/base.py
from abc import ABC, abstractmethod
import pandas as pd

class BaseScorer(ABC):
    """
    The strict contract for all scoring modules.
    """
    @abstractmethod
    def calculate(self, df: pd.DataFrame) -> float:
        """
        Input: Chronologically sorted DataFrame ['open', 'high', 'low', 'close', 'volume']
        Output: A single float between 0.0 and 1.0.
        """
        pass

    def clamp(self, value: float) -> float:
        """Utility to ensure rogue math never breaks the 0.0 to 1.0 bound."""
        if pd.isna(value):
            return 0.0
        return max(0.0, min(1.0, float(value)))