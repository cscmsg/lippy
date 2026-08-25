#!/usr/bin/env python3
"""LocalFlow daemon: holds the models warm, turns audio into finished text.

The daemon exists for one reason: loading Parakeet and a 4B LLM takes about
25 seconds, and nobody will wait 25 seconds to dictate a sentence. Keeping them
resident turns that into ~1.1s per utterance.

It listens on a unix socket in the user's Application Support directory. No TCP
port is opened, so nothing outside this machine can reach it -- which is the
whole premise of the tool.
"""

from __future__ import annotations

import argparse
import collections
import logging
import os
import pathlib
import socket
import socketserver
import sys
import threading
import time

import numpy as np

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import config as config_mod
import protocol
import rules

log = logging.getLogger("localflow.daemon")


class Engine:
    """Models plus the pipeline that runs them. Thread-safe by serialisation.

    MLX evaluation is not safe to run concurrently from multiple threads, and
    there is no reason to: one person dictates one utterance at a time. The
    lock makes that explicit rather than accidental.
    """

    def __init__(self, cfg: config_mod.Config) -> None:
        self.cfg = cfg
        self.lock = threading.Lock()
        self.history: collections.deque[dict] = collections.deque(maxlen=cfg.history_size)

        import asr
        log.info("loading ASR backend %s", cfg.asr_backend)
        self.asr = asr.build(cfg.asr_backend, cfg.asr_model)
        self.asr.warm_up()

        self.polisher = None
        if cfg.polish_enabled:
            import polish
            log.info("loading polish model %s", cfg.polish_model)
            self.polisher = polish.Polisher(cfg.polish_model)
            self.polisher.warm_up()
        log.info("engine ready")

    def open_stream(self):
        """Begin a live-preview session, or None if previews are unavailable.

        The session spans many socket messages, so __enter__/__exit__ are driven
        by hand rather than with a `with` block.
        """
        if not self.cfg.streaming_preview:
            return None
        if not getattr(self.asr, "supports_streaming", False):
            return None
        with self.lock:
            session = self.asr.stream()
            session.__enter__()
            return session

    def feed(self, session, pcm: np.ndarray) -> str:
        import mlx.core as mx
        with self.lock:
            session.add_audio(mx.array(pcm))
            return session.result.text.strip()

    def close_stream(self, session) -> None:
        if session is None:
            return
        with self.lock:
            session.__exit__(None, None, None)

    def process(self, pcm: np.ndarray, mode: str, app: str | None) -> dict:
        with self.lock:
            transcript = self.asr.transcribe(pcm)
            raw_text = transcript.text
            if not raw_text:
                return {
                    "type": "result", "text": "", "raw": "",
                    "asr_ms": round(transcript.compute_s * 1000),
                    "polish_ms": 0, "used_llm": False,
                    "fallback_reason": "no speech detected",
                }

            ruled = rules.clean(raw_text, self.cfg.rule_config())

            polished, used_llm, reason, polish_s = ruled, False, "", 0.0
            if mode == "polish" and self.polisher is not None:
                result = self.polisher.polish(ruled, app_hint=app)
                polished, used_llm = result.text, result.used_llm
                reason, polish_s = result.reason, result.compute_s

            payload = {
                "type": "result", "text": polished, "raw": raw_text,
                "asr_ms": round(transcript.compute_s * 1000),
                "polish_ms": round(polish_s * 1000),
                "used_llm": used_llm, "fallback_reason": reason,
            }
            self.history.append({
                "text": polished, "raw": raw_text, "at": time.time(),
                "duration_s": round(transcript.duration_s, 2),
            })
            log.info("%.1fs audio -> %d chars (asr %dms, polish %dms, llm=%s)",
                     transcript.duration_s, len(polished),
                     payload["asr_ms"], payload["polish_ms"], used_llm)
            return payload


class Handler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        engine: Engine = self.server.engine
        chunks: list[np.ndarray] = []
        mode, app, recording = "polish", None, False
        stream = None

        try:
            for message in protocol.messages(self.request):
                kind = message.get("type")

                if kind == "start":
                    chunks, recording = [], True
                    mode = message.get("mode", "polish")
                    app = message.get("app")
                    stream = engine.open_stream()
                    protocol.send(self.request, {"type": "ready"})

                elif kind == "audio":
                    if recording:
                        pcm = protocol.decode_pcm(message["pcm"])
                        chunks.append(pcm)
                        if stream is not None:
                            # The preview is a convenience. If it fails, drop it
                            # and keep recording -- losing the words someone is
                            # in the middle of saying to salvage a HUD animation
                            # would be a poor trade.
                            try:
                                partial = engine.feed(stream, pcm)
                                if partial:
                                    protocol.send(self.request,
                                                  {"type": "partial", "text": partial})
                            except Exception:
                                log.exception("streaming preview failed; continuing without it")
                                engine.close_stream(stream)
                                stream = None

                elif kind == "cancel":
                    engine.close_stream(stream)
                    stream = None
                    chunks, recording = [], False

                elif kind == "stop":
                    recording = False
                    engine.close_stream(stream)
                    stream = None
                    pcm = np.concatenate(chunks) if chunks else np.zeros(0, dtype=np.float32)
                    chunks = []
                    protocol.send(self.request, engine.process(pcm, mode, app))

                elif kind == "status":
                    protocol.send(self.request, {
                        "type": "status", "ready": True,
                        "asr": engine.asr.name,
                        "polish": engine.cfg.polish_model if engine.polisher else None,
                    })

                elif kind == "last":
                    protocol.send(self.request, {
                        "type": "history", "items": list(engine.history),
                    })

                else:
                    protocol.send(self.request, {
                        "type": "error", "message": f"unknown message type {kind!r}"})

        except (ConnectionResetError, BrokenPipeError):
            pass
        except Exception as exc:  # noqa: BLE001 -- a bad client must not kill the daemon
            log.exception("handler failed")
            try:
                protocol.send(self.request, {"type": "error", "message": str(exc)})
            except OSError:
                pass
        finally:
            # A dropped connection must not leak the session's decoder state.
            try:
                engine.close_stream(stream)
            except Exception:
                log.exception("failed to close streaming session")


class Server(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, path: str, engine: Engine) -> None:
        super().__init__(path, Handler)
        self.engine = engine


def main() -> int:
    parser = argparse.ArgumentParser(description="LocalFlow dictation daemon")
    parser.add_argument("--socket", type=pathlib.Path, default=None)
    parser.add_argument("--no-polish", action="store_true",
                        help="skip loading the LLM (ASR + rules only)")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    config_mod.SUPPORT_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.StreamHandler(sys.stderr),
                  logging.FileHandler(config_mod.LOG_PATH)],
    )

    cfg = config_mod.Config.load()
    if args.no_polish:
        cfg.polish_enabled = False
    if not config_mod.CONFIG_PATH.exists():
        cfg.save()
        log.info("wrote default config to %s", config_mod.CONFIG_PATH)

    path = args.socket or config_mod.socket_path()
    # A socket left behind by a crash would make bind() fail with EADDRINUSE.
    # Only remove it if nothing is actually listening.
    if path.exists():
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            probe.connect(str(path))
            log.error("a daemon is already listening on %s", path)
            return 1
        except OSError:
            path.unlink()
        finally:
            probe.close()

    engine = Engine(cfg)
    server = Server(str(path), engine)
    os.chmod(path, 0o600)  # this socket carries everything the user says
    log.info("listening on %s", path)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("shutting down")
    finally:
        server.server_close()
        path.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
