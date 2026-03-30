from ysparr.modalities.text2text.executor import execute
from ysparr.core.types import PromptRequest


class FakeBackend:
    def __init__(self, chunks):
        self.chunks = chunks

    def stream(self, request):
        for chunk in self.chunks:
            yield chunk


class FakeStorage:
    def __init__(self):
        self.initialized = False
        self.appended = []
        self.finalized = False

    def initialize(self, request):
        self.initialized = True
        return "fake_path"

    def append(self, request, text):
        self.appended.append(text)

    def finalize(self, request):
        self.finalized = True


def make_request():
    return PromptRequest(
        prompt_id="test",
        prompt_text="hello",
        model_name="test",
    )


def test_executor_calls_initialize():
    storage = FakeStorage()
    backend = FakeBackend([])

    execute(make_request(), backend, storage)

    assert storage.initialized is True


def test_executor_appends_all_chunks():
    storage = FakeStorage()
    backend = FakeBackend(["A", "B", "C"])

    execute(make_request(), backend, storage)

    assert storage.appended == ["A", "B", "C"]


def test_executor_calls_finalize():
    storage = FakeStorage()
    backend = FakeBackend([])

    execute(make_request(), backend, storage)

    assert storage.finalized is True


def test_executor_returns_result():
    storage = FakeStorage()
    backend = FakeBackend([])

    result = execute(make_request(), backend, storage)

    assert result.prompt_id == "test"
    assert result.status == "completed"
    assert result.output_path == "fake_path"
