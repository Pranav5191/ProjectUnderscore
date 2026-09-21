from abc import ABC, abstractmethod

class BaseIndicator(ABC):
    def __init__(self, name: str):
        self.name = name
        # Memory is now strictly isolated by security ID
        self.values = {}
        self.history = {}

    def _initialize_state(self, sec_id: str):
        """Allocates an isolated memory block for a new stock on its first tick."""
        if sec_id not in self.values:
            self.values[sec_id] = 0.0
            self.history[sec_id] = []

    @abstractmethod
    def update(self, tick: dict):
        """
        Process new tick data.
        Child classes MUST call sec_id = str(tick['security_id']) 
        and run self._initialize_state(sec_id) before processing math.
        """
        pass

    def get_score(self, sec_id: str) -> float:
        """
        Returns the current calculated score for a specific security.
        The signature now requires a strict memory address lookup.
        """
        # Force string conversion to prevent dictionary key mismatches
        sec_id = str(sec_id)
        return self.values.get(sec_id, 0.0)