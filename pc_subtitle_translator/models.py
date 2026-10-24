from dataclasses import dataclass


@dataclass(frozen=True)
class Caption:
    sequence: int
    source: str
    target: str
    final: bool
    start_seconds: float = 0.0
    end_seconds: float = 0.0
