from pathlib import Path

from ysparr.core.types import PromptRequest
from ysparr.modalities.text2text.executor import execute
from ysparr.modalities.text2text.storage import TextFileStorage


class FakeBackend:
    def __init__(self, chunks):
        self.chunks = chunks

    def stream(self, request):
        for chunk in self.chunks:
            yield chunk


def make_request(prompt_id: str):
    return PromptRequest(
        prompt_id=prompt_id,
        prompt_text="hello",
        model_name="test",
    )


def test_text2text_end_to_end(tmp_path):
    storage = TextFileStorage(tmp_path)
    backend = FakeBackend(["Hello ", "world!"])
    request = make_request("demo")

    execute(request, backend, storage)

    output_file = Path(tmp_path) / "demo.txt"
    content = output_file.read_text()

    assert content == "Hello world!"
