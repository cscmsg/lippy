#!/usr/bin/env python3
"""Command-line client for the LocalFlow daemon.

Exists so the whole pipeline can be exercised, timed and debugged without the
menu-bar app in the way. If dictation is producing bad text, run the same audio
through `flowctl file` and you can see the raw ASR output separately from the
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

import config as config_mod
import protocol

SAMPLE_RATE = 16_000


def connect(path: pathlib.Path) -> socket.socket:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.connect(str(path))
    except (FileNotFoundError, ConnectionRefusedError):
        sys.exit(f"no daemon listening on {path}\n"
                 f"start one with:  make daemon")
    return sock


def load_wav(path: pathlib.Path) -> np.ndarray:
    import soundfile as sf

    pcm, rate = sf.read(str(path), dtype="float32")
    if pcm.ndim > 1:
        pcm = pcm.mean(axis=1)
    if rate != SAMPLE_RATE:
        # Linear index resample. Adequate here because Parakeet's own front end
        # low-passes to a 128-bin mel anyway; use ffmpeg if you need better.
        count = int(len(pcm) * SAMPLE_RATE / rate)
        pcm = np.interp(
            np.linspace(0, len(pcm) - 1, count),
            np.arange(len(pcm)),
            pcm,
        ).astype(np.float32)
    return pcm


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


def main() -> int:
    parser = argparse.ArgumentParser(description="LocalFlow CLI")
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

    p_last = sub.add_parser("last", help="recent utterances held in daemon memory")
    p_last.add_argument("-n", "--count", type=int, default=10)
    p_last.set_defaults(fn=cmd_last)

    args = parser.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
