class TextFileStorage:
    def __init__(self, output_dir: str) -> None:
        pass

    def get_output_path(self, prompt_id: str) -> str:
        pass

    def initialize(self, prompt_id: str) -> str:
        pass

    def append(self, prompt_id: str, text: str) -> None:
        pass

    def finalize(self, prompt_id: str) -> None:
        pass
