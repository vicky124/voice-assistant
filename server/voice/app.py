"""FastAPI app: WebSocket endpoint plus the built web client."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles

from voice.providers import LLM, TTS, ClaudeLLM, DeepgramSTT, EchoLLM, PollyTTS
from voice.session import VoiceSession

log = logging.getLogger(__name__)
WEB_DIST = Path(__file__).resolve().parents[2] / "web" / "dist"


class Providers:
    """Picks real providers when credentials exist, otherwise browser/offline fallbacks."""

    def __init__(self, llm: LLM | None = None, tts: TTS | None = None, stt_factory=None):
        self.llm = llm or (ClaudeLLM(os.environ.get("VOICE_MODEL", "claude-opus-5-5"),
                                     os.environ.get("VOICE_EFFORT", "low"))
                           if os.environ.get("ANTHROPIC_API_KEY") else EchoLLM())
        if tts is None and os.environ.get("TTS_PROVIDER", "").lower() == "polly":
            tts = PollyTTS(os.environ.get("POLLY_VOICE", "Joanna"))
        self.tts = tts
        if stt_factory is None and (key := os.environ.get("DEEPGRAM_API_KEY")):
            stt_factory = lambda: DeepgramSTT(key).connect()  # noqa: E731
        self.stt_factory = stt_factory


def create_app(providers: Providers | None = None) -> FastAPI:
    providers = providers or Providers()
    app = FastAPI(title="Voice assistant")

    @app.get("/healthz")
    async def healthz():
        return {"status": "ok"}

    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket):
        await ws.accept()
        lock = asyncio.Lock()  # one writer at a time: tokens, audio and STT events interleave

        async def send_json(msg: dict) -> None:
            async with lock:
                await ws.send_text(json.dumps(msg))

        async def send_bytes(data: bytes) -> None:
            async with lock:
                await ws.send_bytes(data)

        session = VoiceSession(send_json, send_bytes, providers.llm, providers.tts)
        stt = None
        stt_task: asyncio.Task | None = None
        await send_json({"type": "ready", "llm": providers.llm.name,
                         "server_stt": providers.stt_factory is not None,
                         "server_tts": providers.tts is not None,
                         "tts_sample_rate": getattr(providers.tts, "sample_rate", None)})
        try:
            while True:
                msg = await ws.receive()
                if msg["type"] == "websocket.disconnect":
                    break
                if msg.get("bytes") is not None:
                    if stt is None and providers.stt_factory:
                        stt = await providers.stt_factory()
                        stt_task = asyncio.create_task(session.pump_stt(stt))
                    if stt:
                        await stt.send(msg["bytes"])
                    continue
                data = json.loads(msg.get("text") or "{}")
                if data.get("type") == "user_text":
                    await send_json({"type": "transcript", "text": data.get("text", ""), "final": True})
                    await session.user_said(data.get("text", ""))
                elif data.get("type") == "interrupt":
                    await session.interrupt(data.get("played_index"))
        except WebSocketDisconnect:
            pass
        finally:
            await session.close()
            if stt_task:
                stt_task.cancel()
            if stt:
                with contextlib.suppress(Exception):
                    await stt.close()

    if WEB_DIST.exists():
        app.mount("/", StaticFiles(directory=WEB_DIST, html=True), name="web")
    return app


def app_factory() -> FastAPI:  # uvicorn voice.app:app_factory --factory
    logging.basicConfig(level=logging.INFO)
    return create_app()
