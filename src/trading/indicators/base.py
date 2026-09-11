from abc import ABC, abstractmethod

class BaseIndicator(ABC):
    def __init__(self, name: str):
        self.name = name
        self.value = 0.0

    @abstractmethod
    def update(self, tick: dict):
        """
        Process new tick data and update self.value.
        Must be implemented by all child indicators.
        """
        pass

    def get_score(self) -> float:
        """Returns the current calculated score (-1.0 to 1.0)"""
        return self.value