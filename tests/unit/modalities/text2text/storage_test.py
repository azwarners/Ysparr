import time
from pathlib import Path

import pytest

from ysparr.core.types import PromptRequest
from ysparr.modalities.text2text.storage import TextFileStorage
from ysparr.core.exceptions import StorageError


def make_request(prompt_id: str, **metadata):
    return PromptRequest(
        prompt_id=prompt_id,
        prompt_text="test",
        model_name="test",
        metadata=metadata,
    )


def test_initialize_creates_file(tmp_path):
    storage = TextFileStorage(tmp_path)
    request = make_request("test1")

    path = storage.initialize(request)

    assert Path(path).exists()


def test_append_writes_text(tmp_path):
    storage = TextFileStorage(tmp_path)
    request = make_request("test2")

    storage.initialize(request)
    storage.append(request, "hello")

    content = Path(storage.get_output_path("test2")).read_text()
    assert content == "hello"


def test_append_is_incremental(tmp_path):
    storage = TextFileStorage(tmp_path)
    request = make_request("test3")

    storage.initialize(request)
    storage.append(request, "A")
    storage.append(request, "B")

    content = Path(storage.get_output_path("test3")).read_text()
    assert content == "AB"


def test_finalize_missing_file_raises(tmp_path):
    storage = TextFileStorage(tmp_path)
    request = make_request("missing")

    with pytest.raises(StorageError):
        storage.finalize(request)


def test_persist_false_skips_storage(tmp_path):
    storage = TextFileStorage(tmp_path)
    request = make_request("nopersist", persist=False)

    path = storage.initialize(request)

    assert path == ""
    assert not Path(tmp_path / "nopersist.txt").exists()


def test_max_files_retention(tmp_path):
    storage = TextFileStorage(tmp_path, max_files=2)

    r1 = make_request("a")
    r2 = make_request("b")
    r3 = make_request("c")

    storage.initialize(r1)
    storage.finalize(r1)

    time.sleep(0.01)

    storage.initialize(r2)
    storage.finalize(r2)

    time.sleep(0.01)

    storage.initialize(r3)
    storage.finalize(r3)

    files = list(tmp_path.glob("*.txt"))
    assert len(files) == 2


def test_max_age_retention(tmp_path):
    storage = TextFileStorage(tmp_path)

    request = make_request("old", max_age_seconds=0)

    storage.initialize(request)
    storage.finalize(request)

    files = list(tmp_path.glob("*.txt"))
    assert len(files) == 0
