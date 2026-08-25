"""Wire protocol: newline-delimited JSON over a unix domain socket.

Audio rides as base64 inside the JSON. That costs 33% over raw bytes, which on
a unix socket carrying 16 kHz mono int16 (32 KB/s) is 11 KB/s of overhead --
irrelevant, and it buys a protocol you can read with `nc` when something breaks.

Client -> daemon:
  {"type": "start",  "mode": "polish"|"raw", "app": "Slack"}
  {"type": "audio",  "pcm": "<base64 int16 little-endian, 16 kHz mono>"}
  {"type": "stop"}
  {"type": "cancel"}
  {"type": "status"}
  {"type": "last"}

Daemon -> client:
  {"type": "ready"}
  {"type": "result", "text": ..., "raw": ..., "asr_ms": .., "polish_ms": ..,
   "used_llm": bool, "fallback_reason": ...}
  {"type": "status", "ready": bool, "asr": ..., "polish": ...}
  {"type": "error",  "message": ...}
"""

from __future__ import annotations

import base64
import json
import socket

import numpy as np


def encode_pcm(pcm: np.ndarray) -> str:
    """float32 in [-1, 1] -> base64 int16 little-endian."""
    clipped = np.clip(pcm, -1.0, 1.0)
    return base64.b64encode((clipped * 32767).astype("<i2").tobytes()).decode("ascii")


def decode_pcm(payload: str) -> np.ndarray:
    raw = np.frombuffer(base64.b64decode(payload), dtype="<i2")
    return (raw.astype(np.float32) / 32767.0).copy()


def send(sock: socket.socket, message: dict) -> None:
    sock.sendall((json.dumps(message) + "\n").encode("utf-8"))


def messages(sock: socket.socket):
    """Yield decoded JSON messages from a socket until it closes."""
    buffer = b""
    while True:
        chunk = sock.recv(65536)
        if not chunk:
            return
        buffer += chunk
        while b"\n" in buffer:
            line, buffer = buffer.split(b"\n", 1)
            if line.strip():
                yield json.loads(line)
