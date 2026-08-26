"""Fetching the ONNX speech model.

The macOS build pulls its MLX model through Hugging Face's own cache the first
time it runs. The ONNX build has no equivalent, so this module is that step:
one archive from the sherpa-onnx release, resumed if the connection drops,
unpacked atomically, and never fetched twice.

Stdlib only, deliberately. The rules tests run on a bare runner with nothing
installed but pytest, and a model fetcher that dragged numpy in at import time
would quietly end that.
"""

from __future__ import annotations

import logging
import os
import pathlib
import shutil
import tarfile
import tempfile
import urllib.request

log = logging.getLogger("lippy.models")

MODEL_NAME = "sherpa-onnx-nemo-parakeet-tdt-0.6b-v3-int8"
MODEL_URL = (
    "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/"
    f"{MODEL_NAME}.tar.bz2"
)

# What SherpaBackend actually opens. A directory that exists is not a directory
# that is finished, and "the folder is there" is the check that turns a killed
# download into a decode failure three layers away.
REQUIRED_FILES = (
    "encoder.int8.onnx",
    "decoder.int8.onnx",
    "joiner.int8.onnx",
    "tokens.txt",
)

CACHE_ROOT = pathlib.Path.home() / ".cache" / "lippy-onnx"


def model_dir(override: str | os.PathLike | None = None) -> pathlib.Path:
    """Where the unpacked model lives. Argument, then environment, then cache.

    Single-sourced here because asr.py resolves the same path to load the model
    and this module resolves it to write one, and the two drifting apart means
    downloading to a place the loader never looks.
    """
    if override:
        return pathlib.Path(override)
    from_env = os.environ.get("LIPPY_ONNX_MODEL_DIR")
    if from_env:
        return pathlib.Path(from_env)
    return CACHE_ROOT / MODEL_NAME


def is_complete(directory: pathlib.Path) -> bool:
    """True only when every file the recogniser opens is present and non-empty.

    Zero-length is treated as absent because that is what a disk that filled up
    mid-unpack leaves behind, and it is indistinguishable from success to any
    check that only asks whether the path exists.
    """
    return directory.is_dir() and all(
        (directory / name).is_file() and (directory / name).stat().st_size > 0
        for name in REQUIRED_FILES
    )


def _download(url: str, dest: pathlib.Path, opener=None, progress=None,
              chunk: int = 1 << 20) -> pathlib.Path:
    """Fetch `url` to `dest`, resuming a previous partial transfer if there is one.

    The partial lives beside the destination as `.part` and is only renamed
    into place once the transfer completes, so an interrupted run never leaves
    something that looks like a finished archive.
    """
    opener = opener or urllib.request.urlopen
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    have = part.stat().st_size if part.exists() else 0

    request = urllib.request.Request(url)
    if have:
        request.add_header("Range", f"bytes={have}-")

    with opener(request) as response:
        status = getattr(response, "status", None) or getattr(response, "code", 200)
        if have and status != 206:
            # The server ignored the range header and is sending the whole file
            # again. Appending would splice a second copy onto the first and
            # produce an archive that fails to unpack for a reason that points
            # nowhere near the resume logic.
            log.info("server ignored resume (HTTP %s); restarting download", status)
            have = 0

        declared = response.headers.get("Content-Length") if response.headers else None
        total = (int(declared) + have) if declared and declared.isdigit() else None

        done = have
        with open(part, "ab" if have else "wb") as handle:
            while True:
                block = response.read(chunk)
                if not block:
                    break
                handle.write(block)
                done += len(block)
                if progress:
                    progress(done, total)

    if total is not None and done != total:
        # Truncated transfers are the common case on a flaky link, and a
        # truncated bz2 raises somewhere unhelpful. Keep the .part so the next
        # run resumes rather than starting the 487 MB again.
        raise IOError(f"incomplete download: {done} of {total} bytes from {url}")

    part.replace(dest)
    return dest


def _unpack(archive: pathlib.Path, into: pathlib.Path) -> pathlib.Path:
    """Extract `archive` so that `into` ends up holding the model files.

    Unpacks to a sibling temporary directory and renames, because extracting
    straight into the destination means a cancelled unpack leaves a directory
    that a later run has no way to tell from a finished one.
    """
    into.parent.mkdir(parents=True, exist_ok=True)
    staging = pathlib.Path(tempfile.mkdtemp(dir=into.parent, prefix=".unpack-"))
    try:
        with tarfile.open(archive, "r:bz2") as tar:
            # filter="data" refuses absolute paths, parent traversal, links and
            # device nodes. It is the default from Python 3.14 and a warning
            # before that, so it is named explicitly rather than inherited.
            tar.extractall(staging, filter="data")

        # The release archive carries one top-level directory. Anything else is
        # a different archive than the one this code was written against.
        entries = [child for child in staging.iterdir() if not child.name.startswith(".")]
        if len(entries) == 1 and entries[0].is_dir():
            extracted = entries[0]
        else:
            extracted = staging

        if into.exists():
            shutil.rmtree(into)
        extracted.replace(into)
        return into
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def ensure(override: str | os.PathLike | None = None, progress=None,
           opener=None, url: str = MODEL_URL) -> pathlib.Path:
    """Return the model directory, downloading and unpacking it if it is absent.

    Idempotent: a complete directory short-circuits before any network call, so
    this is safe to call on every start.
    """
    directory = model_dir(override)
    if is_complete(directory):
        return directory

    log.info("fetching %s into %s", MODEL_NAME, directory)
    archive = directory.parent / f"{MODEL_NAME}.tar.bz2"
    _download(url, archive, opener=opener, progress=progress)
    _unpack(archive, directory)
    archive.unlink(missing_ok=True)

    if not is_complete(directory):
        missing = [n for n in REQUIRED_FILES if not (directory / n).is_file()]
        raise IOError(f"unpacked {MODEL_NAME} is missing {missing} in {directory}")
    log.info("model ready at %s", directory)
    return directory


def _human(done: int, total: int | None) -> str:
    if not total:
        return f"{done / 1e6:.0f} MB"
    return f"{done / 1e6:.0f} of {total / 1e6:.0f} MB ({done * 100 // total}%)"


if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    def show(done, total):
        print(f"\r  {_human(done, total)}", end="", flush=True)

    try:
        path = ensure(progress=show)
    except Exception as error:                      # noqa: BLE001 - reported, not swallowed
        print(f"\nfailed: {error}", file=sys.stderr)
        raise SystemExit(1)
    print(f"\n{path}")
