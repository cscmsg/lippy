"""Tests for the ONNX model bootstrap.

Weighted towards the ways a 487 MB download goes wrong rather than the way it
goes right: a dropped connection, a server that ignores a range request, a
disk that fills mid-unpack, an archive that is not the one we expected.
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "daemon"))

import io
import tarfile

import pytest
import models

BODY = b"".join(bytes([i % 256]) for i in range(4096))


class FakeResponse:
    """A urlopen result. Headers are a plain dict so a test can omit or break one."""

    def __init__(self, body: bytes, status: int = 200, headers=None):
        self._body = body
        self.status = status
        self.headers = {"Content-Length": str(len(body))} if headers is None else headers
        self._pos = 0

    def read(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            chunk, self._pos = self._body[self._pos:], len(self._body)
            return chunk
        chunk = self._body[self._pos:self._pos + size]
        self._pos += len(chunk)
        return chunk

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class RecordingOpener:
    """Stands in for urlopen, records every request, and refuses a surprise call."""

    def __init__(self, *responses):
        self._responses = list(responses)
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        if not self._responses:
            raise AssertionError("opener called more times than the test allowed")
        return self._responses.pop(0)

    @property
    def calls(self) -> int:
        return len(self.requests)


def _archive(tmp_path: pathlib.Path, members: dict, top: str | None = None) -> pathlib.Path:
    path = tmp_path / "model.tar.bz2"
    with tarfile.open(path, "w:bz2") as tar:
        for name, payload in members.items():
            info = tarfile.TarInfo(f"{top}/{name}" if top else name)
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))
    return path


def _complete_members() -> dict:
    return {name: b"x" * 32 for name in models.REQUIRED_FILES}


# ---- path resolution ----------------------------------------------------

def test_explicit_override_wins_over_environment(monkeypatch):
    monkeypatch.setenv("LIPPY_ONNX_MODEL_DIR", "/from/env")
    assert models.model_dir("/explicit") == pathlib.Path("/explicit")


def test_environment_used_when_no_argument(monkeypatch):
    monkeypatch.setenv("LIPPY_ONNX_MODEL_DIR", "/from/env")
    assert models.model_dir() == pathlib.Path("/from/env")


def test_cache_default_when_nothing_set(monkeypatch):
    monkeypatch.delenv("LIPPY_ONNX_MODEL_DIR", raising=False)
    assert models.model_dir() == models.CACHE_ROOT / models.MODEL_NAME


def test_empty_override_is_treated_as_unset(monkeypatch):
    """An exported-but-empty value must not resolve the model to the cwd."""
    monkeypatch.delenv("LIPPY_ONNX_MODEL_DIR", raising=False)
    assert models.model_dir("") == models.CACHE_ROOT / models.MODEL_NAME


def test_empty_environment_variable_is_treated_as_unset(monkeypatch):
    monkeypatch.setenv("LIPPY_ONNX_MODEL_DIR", "")
    assert models.model_dir() == models.CACHE_ROOT / models.MODEL_NAME


def test_path_object_accepted_as_override(tmp_path):
    assert models.model_dir(tmp_path) == tmp_path


# ---- completeness -------------------------------------------------------

def test_complete_directory_recognised(tmp_path):
    for name in models.REQUIRED_FILES:
        (tmp_path / name).write_bytes(b"x")
    assert models.is_complete(tmp_path) is True


@pytest.mark.parametrize("missing", models.REQUIRED_FILES)
def test_any_missing_file_means_incomplete(tmp_path, missing):
    for name in models.REQUIRED_FILES:
        if name != missing:
            (tmp_path / name).write_bytes(b"x")
    assert models.is_complete(tmp_path) is False


def test_zero_length_file_means_incomplete(tmp_path):
    """What a disk that filled up mid-unpack leaves behind."""
    for name in models.REQUIRED_FILES:
        (tmp_path / name).write_bytes(b"x")
    (tmp_path / models.REQUIRED_FILES[0]).write_bytes(b"")
    assert models.is_complete(tmp_path) is False


def test_absent_directory_is_incomplete(tmp_path):
    assert models.is_complete(tmp_path / "nope") is False


def test_file_where_directory_expected_is_incomplete(tmp_path):
    target = tmp_path / "model"
    target.write_bytes(b"not a directory")
    assert models.is_complete(target) is False


# ---- download -----------------------------------------------------------

def test_download_writes_the_body_and_clears_the_partial(tmp_path):
    opener = RecordingOpener(FakeResponse(BODY))
    dest = tmp_path / "model.tar.bz2"

    models._download("https://example.invalid/m", dest, opener=opener)

    assert dest.read_bytes() == BODY
    assert not dest.with_name(dest.name + ".part").exists()
    assert opener.calls == 1
    assert "Range" not in opener.requests[0].headers


def test_download_resumes_from_a_partial(tmp_path):
    dest = tmp_path / "model.tar.bz2"
    part = dest.with_name(dest.name + ".part")
    part.write_bytes(BODY[:1000])
    opener = RecordingOpener(FakeResponse(BODY[1000:], status=206))

    models._download("https://example.invalid/m", dest, opener=opener)

    assert dest.read_bytes() == BODY
    # urllib title-cases header names as they are added.
    assert opener.requests[0].headers.get("Range") == "bytes=1000-"


def test_download_restarts_when_server_ignores_the_range(tmp_path):
    """A 200 to a ranged request means the whole file is coming again.

    Appending it to the partial splices two copies together and yields an
    archive that fails to unpack for a reason pointing nowhere near here.
    """
    dest = tmp_path / "model.tar.bz2"
    part = dest.with_name(dest.name + ".part")
    part.write_bytes(BODY[:1000])
    opener = RecordingOpener(FakeResponse(BODY, status=200))

    models._download("https://example.invalid/m", dest, opener=opener)

    assert dest.read_bytes() == BODY
    assert len(dest.read_bytes()) == len(BODY), "partial must not be prepended"


def test_truncated_download_raises_and_keeps_the_partial(tmp_path):
    dest = tmp_path / "model.tar.bz2"
    short = FakeResponse(BODY[:500], headers={"Content-Length": str(len(BODY))})
    opener = RecordingOpener(short)

    with pytest.raises(IOError, match="incomplete download"):
        models._download("https://example.invalid/m", dest, opener=opener)

    part = dest.with_name(dest.name + ".part")
    assert part.read_bytes() == BODY[:500], "keep the bytes so the retry resumes"
    assert not dest.exists()


def test_download_without_content_length_still_completes(tmp_path):
    opener = RecordingOpener(FakeResponse(BODY, headers={}))
    dest = tmp_path / "model.tar.bz2"

    models._download("https://example.invalid/m", dest, opener=opener)

    assert dest.read_bytes() == BODY


def test_download_with_unparseable_content_length_still_completes(tmp_path):
    opener = RecordingOpener(FakeResponse(BODY, headers={"Content-Length": "many"}))
    dest = tmp_path / "model.tar.bz2"

    models._download("https://example.invalid/m", dest, opener=opener)

    assert dest.read_bytes() == BODY


def test_download_creates_missing_parent_directory(tmp_path):
    opener = RecordingOpener(FakeResponse(BODY))
    dest = tmp_path / "a" / "b" / "model.tar.bz2"

    models._download("https://example.invalid/m", dest, opener=opener)

    assert dest.read_bytes() == BODY


# ---- unpack -------------------------------------------------------------

def test_unpack_strips_the_top_level_directory(tmp_path):
    archive = _archive(tmp_path, _complete_members(), top=models.MODEL_NAME)
    into = tmp_path / "out" / models.MODEL_NAME

    models._unpack(archive, into)

    assert models.is_complete(into)
    assert not (into / models.MODEL_NAME).exists(), "must not nest the top directory"


def test_unpack_handles_a_flat_archive(tmp_path):
    archive = _archive(tmp_path, _complete_members())
    into = tmp_path / "out" / models.MODEL_NAME

    models._unpack(archive, into)

    assert models.is_complete(into)


def test_unpack_leaves_no_staging_directories(tmp_path):
    archive = _archive(tmp_path, _complete_members(), top=models.MODEL_NAME)
    into = tmp_path / "out" / models.MODEL_NAME

    models._unpack(archive, into)

    strays = [p.name for p in into.parent.iterdir() if p.name.startswith(".unpack-")]
    assert strays == []


def test_unpack_replaces_an_incomplete_previous_attempt(tmp_path):
    into = tmp_path / "out" / models.MODEL_NAME
    into.mkdir(parents=True)
    (into / "encoder.int8.onnx").write_bytes(b"")
    (into / "leftover.tmp").write_bytes(b"junk")
    archive = _archive(tmp_path, _complete_members(), top=models.MODEL_NAME)

    models._unpack(archive, into)

    assert models.is_complete(into)
    assert not (into / "leftover.tmp").exists(), "stale files must not survive"


def test_unpack_refuses_a_traversing_member(tmp_path):
    archive = _archive(tmp_path, {"../escaped.txt": b"owned"})
    into = tmp_path / "out" / models.MODEL_NAME

    with pytest.raises(tarfile.TarError):
        models._unpack(archive, into)

    assert not (tmp_path / "escaped.txt").exists()
    assert not (into.parent / "escaped.txt").exists()


# ---- ensure -------------------------------------------------------------

def test_ensure_short_circuits_on_a_complete_directory(tmp_path):
    for name in models.REQUIRED_FILES:
        (tmp_path / name).write_bytes(b"x")
    opener = RecordingOpener()

    assert models.ensure(tmp_path, opener=opener) == tmp_path
    assert opener.calls == 0, "a complete model must not touch the network"


def test_ensure_downloads_unpacks_and_removes_the_archive(tmp_path):
    (tmp_path / "src").mkdir()
    payload = _archive(tmp_path / "src", _complete_members(),
                       top=models.MODEL_NAME).read_bytes()
    into = tmp_path / "cache" / models.MODEL_NAME
    opener = RecordingOpener(FakeResponse(payload))

    result = models.ensure(into, opener=opener, url="https://example.invalid/m")

    assert result == into
    assert models.is_complete(into)
    assert not (into.parent / f"{models.MODEL_NAME}.tar.bz2").exists(), \
        "the archive is 487 MB and has no reason to stay"
    assert opener.calls == 1


def test_ensure_reports_progress(tmp_path):
    (tmp_path / "src").mkdir()
    payload = _archive(tmp_path / "src", _complete_members(), top=models.MODEL_NAME).read_bytes()
    seen = []
    opener = RecordingOpener(FakeResponse(payload))

    models.ensure(tmp_path / "cache" / models.MODEL_NAME, progress=lambda d, t: seen.append((d, t)),
                  opener=opener, url="https://example.invalid/m")

    assert seen, "progress callback was never invoked"
    assert seen[-1][0] == len(payload)
    assert seen[-1][1] == len(payload)


def test_ensure_rejects_an_archive_missing_model_files(tmp_path):
    """A well-formed tar of the wrong thing must not read as a working model."""
    (tmp_path / "src").mkdir()
    payload = _archive(tmp_path / "src", {"README.md": b"wrong archive"},
                       top=models.MODEL_NAME).read_bytes()
    into = tmp_path / "cache" / models.MODEL_NAME
    opener = RecordingOpener(FakeResponse(payload))

    with pytest.raises(IOError, match="missing"):
        models.ensure(into, opener=opener, url="https://example.invalid/m")


def test_model_url_points_at_the_pinned_release_asset():
    """Hard-coded rather than rebuilt from MODEL_NAME, so a rename is caught."""
    assert models.MODEL_URL == (
        "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/"
        "sherpa-onnx-nemo-parakeet-tdt-0.6b-v3-int8.tar.bz2"
    )
