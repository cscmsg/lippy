#!/usr/bin/env python3
"""Run one audio file through the pipeline, in this process.

`lippyctl file` makes the same journey over the daemon socket, which is a macOS
arrangement. Windows is a single process with no socket, so this is that
journey without one: load a WAV, transcribe it, apply the rules for a cleanup
level, and print the raw transcript beside the final text.

Keeping those two separate is the point. A disappointing result is either a
mishearing or an over-edit, and the fix for one is no help against the other.

This is also what CI runs on Windows, because a job that imports the modules
and stops has not shown that the ONNX runtime can decode anything.
"""

from __future__ import annotations

import argparse
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import asr
import config as config_mod
import rules


def run(path: pathlib.Path, level: str | None = None, backend: str | None = None,
        model: str | None = None) -> dict:
    """Transcribe `path` and apply the dial. Returns the stages, not just the end."""
    cfg = config_mod.Config()
    level = level or cfg.cleanup_level
    if level not in config_mod.CLEANUP_LEVELS:
        raise ValueError(f"unknown cleanup level {level!r}, expected one of "
                         f"{', '.join(config_mod.CLEANUP_LEVELS)}")

    started = time.perf_counter()
    engine = asr.build(backend or cfg.asr_backend, model or cfg.asr_model)
    load_s = time.perf_counter() - started

    transcript = engine.transcribe(asr.load_wav(path))

    # Mirrors lippyd: "raw" means raw, and every other level runs the rules the
    # dial shapes. Duplicating the branch rather than sharing it would be a
    # second place for the dial to mean something slightly different.
    if level == "raw":
        ruled = transcript.text
    else:
        ruled = rules.clean(transcript.text, cfg.rule_config(level))

    final, used_llm, reason = ruled, False, ""
    if level == "polish":
        import polish

        polisher = polish.Polisher(cfg.polish_model)
        result = polisher.polish(ruled)
        final, used_llm, reason = result.text, result.used_llm, result.reason

    return {
        "backend": engine.name,
        "level": level,
        "raw": transcript.text,
        "final": final,
        "used_llm": used_llm,
        "reason": reason,
        "audio_s": transcript.duration_s,
        "load_s": load_s,
        "asr_s": transcript.compute_s,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("audio", type=pathlib.Path)
    parser.add_argument("--level", choices=config_mod.CLEANUP_LEVELS,
                        help="cleanup dial (default: this platform's default)")
    parser.add_argument("--backend", help="asr backend (default: this platform's default)")
    parser.add_argument("--model", help="model id or directory for the backend")
    parser.add_argument("--expect", action="append", default=[], metavar="WORD",
                        help="require this word in the final text; repeatable")
    args = parser.parse_args()

    if not args.audio.is_file():
        print(f"no such audio file: {args.audio}", file=sys.stderr)
        return 2

    result = run(args.audio, args.level, args.backend, args.model)

    print(f"backend  {result['backend']} at level {result['level']}")
    print(f"audio    {result['audio_s']:.2f}s")
    print(f"load     {result['load_s']:.1f}s")
    print(f"asr      {result['asr_s']:.2f}s")
    print(f"raw      {result['raw']!r}")
    print(f"final    {result['final']!r}")
    if result["reason"]:
        print(f"fallback {result['reason']}")

    if not result["final"].strip():
        print("FAIL: the pipeline produced no text", file=sys.stderr)
        return 1

    # Case-insensitive because capitalisation is the rules pass's business and
    # is asserted properly in the rules tests, not here.
    lowered = result["final"].lower()
    missing = [word for word in args.expect if word.lower() not in lowered]
    if missing:
        print(f"FAIL: expected {missing} in {result['final']!r}", file=sys.stderr)
        return 1

    print("ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
