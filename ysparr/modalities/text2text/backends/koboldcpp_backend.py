from collections.abc import Iterator
from ysparr.core.types import PromptRequest

class KoboldCPPBackend:
    def __init__(self, endpoint: str) -> None:
        pass

    def stream(self, request: PromptRequest) -> Iterator[str]:
        pass
