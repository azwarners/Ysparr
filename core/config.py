from dataclasses import dataclass

@dataclass(frozen=True)
class YsparrConfig:
    debug: bool = False
