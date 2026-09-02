#!/usr/bin/env python3
"""Command-line client for the Lippy daemon.

Exists so the whole pipeline can be exercised, timed and debugged without the
menu-bar app in the way. If dictation is producing bad text, run the same audio
through `lippyctl file` and you can see the raw ASR output separately from the
polished output, and whether the guardrails fired.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import socket
import sys
import time

import numpy as np

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import asr
import config as config_mod
import protocol
import terms as terms_mod
from asr import load_wav  # noqa: F401  (kept importable from here)

SAMPLE_RATE = 16_000


def connect(path: pathlib.Path) -> socket.socket:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.connect(str(path))
    except (FileNotFoundError, ConnectionRefusedError):
        sys.exit(f"no daemon listening on {path}\n"
                 f"start one with:  make daemon")
    return sock


def send_audio(sock: socket.socket, pcm: np.ndarray, mode: str, app: str | None) -> dict:
    protocol.send(sock, {"type": "start", "mode": mode, "app": app})
    stream = protocol.messages(sock)
    next(stream)  # the "ready" acknowledgement

    # Chunked to mirror how the app streams live audio.
    for start in range(0, len(pcm), SAMPLE_RATE):
        protocol.send(sock, {"type": "audio",
                             "pcm": protocol.encode_pcm(pcm[start:start + SAMPLE_RATE])})
    protocol.send(sock, {"type": "stop"})

    # Skip anything that is not the result, rather than assuming the next
    # message is one.
    for message in stream:
        if message.get("type") == "result":
            return message
    raise SystemExit("daemon closed the connection before returning a result")


def cmd_status(args) -> int:
    sock = connect(args.socket)
    protocol.send(sock, {"type": "status"})
    print(json.dumps(next(protocol.messages(sock)), indent=2))
    return 0


def cmd_file(args) -> int:
    pcm = load_wav(args.path)
    sock = connect(args.socket)
    t0 = time.perf_counter()
    level = args.level or ("raw" if args.raw else "polish")
    result = send_audio(sock, pcm, level, args.app)
    wall = time.perf_counter() - t0

    if args.json:
        print(json.dumps(result, indent=2))
        return 0

    print(f"audio      {len(pcm) / SAMPLE_RATE:.2f}s")
    print(f"asr        {result['asr_ms']}ms")
    print(f"polish     {result['polish_ms']}ms"
          + ("" if result["used_llm"] else f"  (fell back: {result['fallback_reason']})"))
    print(f"round trip {wall * 1000:.0f}ms\n")
    print(f"raw    : {result['raw']}")
    print(f"final  : {result['text']}")
    return 0


def cmd_last(args) -> int:
    sock = connect(args.socket)
    protocol.send(sock, {"type": "last"})
    items = next(protocol.messages(sock))["items"]
    if not items:
        print("no utterances yet")
        return 0
    for item in items[-args.count:]:
        stamp = time.strftime("%H:%M:%S", time.localtime(item["at"]))
        print(f"[{stamp}] ({item['duration_s']}s) {item['text']}")
    return 0


# How many collisions before an entry is called out rather than just reported.
NOISY_TERM = 5


def cmd_terms(args) -> int:
    """Report what each protected term would rewrite, before it is trusted.

    A protected term is a fuzzy match, so its safety is a property of the term
    and not of the setting. This measures it against the system word list rather
    than leaving it to be discovered in pasted text.
    """
    cfg = config_mod.Config.load()
    entries = args.check or cfg.protected_terms
    threshold = args.threshold or cfg.fuzzy_threshold

    if not entries:
        print("no protected_terms configured\n"
              f"add some to {config_mod.CONFIG_PATH}, or pass --check to try one")
        return 0

    words = terms_mod.load_wordlist()
    if words is None:
        # Saying so beats printing a clean bill of health this cannot support.
        print("no system word list on this platform "
              f"(looked in {', '.join(terms_mod.WORDLIST_PATHS)})")
        print("terms are listed without a collision count:\n")
        for entry in entries:
            print(f"  {entry}")
        return 1

    relaxed = terms_mod.url_threshold(threshold)
    print(f"{len(words):,} words, prose threshold {threshold:.2f}, "
          f"host threshold {relaxed:.2f}\n")

    worst = 0
    for entry in entries:
        hits = terms_mod.audit(entry, words, threshold)
        in_hosts = terms_mod.audit(entry, words, relaxed)
        worst = max(worst, len(hits))
        if not hits:
            note = "clear"
        else:
            shown = ", ".join(hits[:6]) + (", ..." if len(hits) > 6 else "")
            note = f"{len(hits)} would be rewritten: {shown}"
        flag = "  " if len(hits) < NOISY_TERM else "! "
        print(f"{flag}{entry:<24} {note}")
        if len(in_hosts) != len(hits):
            print(f"  {'':<24} {len(in_hosts)} inside a hostname")

    if worst >= NOISY_TERM:
        print(f"\nEntries marked ! collide with {NOISY_TERM} or more real words. "
              f"A term that looks like ordinary English is a poor protected term, "
              f"and raising the threshold trades away the mis-hearings it was "
              f"added to catch. Prefer a dictionary entry for those.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Lippy CLI")
    parser.add_argument("--socket", type=pathlib.Path, default=config_mod.socket_path())
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="daemon health and loaded models").set_defaults(fn=cmd_status)

    p_file = sub.add_parser("file", help="run an audio file through the pipeline")
    p_file.add_argument("path", type=pathlib.Path)
    p_file.add_argument("--level", default=None,
                        choices=("raw", "fillers", "clean", "polish"),
                        help="cleanup dial")
    p_file.add_argument("--raw", action="store_true", help="shorthand for --level raw")
    p_file.add_argument("--app", help="pretend the text is destined for this app")
    p_file.add_argument("--json", action="store_true")
    p_file.set_defaults(fn=cmd_file)

    p_terms = sub.add_parser("terms", help="audit protected terms for collisions")
    p_terms.add_argument("--check", action="append", metavar="TERM",
                         help="audit this term instead of the configured ones")
    p_terms.add_argument("--threshold", type=float, default=None)
    p_terms.set_defaults(fn=cmd_terms)

    p_last = sub.add_parser("last", help="recent utterances held in daemon memory")
    p_last.add_argument("-n", "--count", type=int, default=10)
    p_last.set_defaults(fn=cmd_last)

    args = parser.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
