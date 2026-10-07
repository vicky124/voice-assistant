"""Pluggable LLM, speech-to-text and text-to-speech providers."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urlencode

log = logging.getLogger(__name__)

VOICE_SYSTEM_PROMPT = """\
You are a friendly voice assistant. Everything you write is converted to speech.
Latency-sensitive; begin your visible answer immediately.
- Answer in one to three short sentences unless the user asks for detail.
- Plain spoken language only: no markdown, lists, headings, code blocks, emoji, or URLs.
- Spell out symbols and units the way a person would say them.
- If you didn't catch what the user meant, ask a short clarifying question."""


# ---- LLM ------------------------------------------------------------------------

class LLM(Protocol):
    name: str

    def stream(self, history: list[dict]) -> AsyncIterator[str]: ...


class ClaudeLLM:
    def __init__(self, model: str = "claude-opus-5-5", effort: str = "low", client: Any | None = None):
        import anthropic

        self.name = model
        self.effort = effort
        self.client = client or anthropic.AsyncAnthropic()

    async def stream(self, history: list[dict]) -> AsyncIterator[str]:
        async with self.client.beta.messages.stream(
            model=self.name,
            max_tokens=1_024,
            system=VOICE_SYSTEM_PROMPT,
            messages=history,
            # Low effort keeps time-to-first-token short; voice turns are conversational.
            output_config={"effort": self.effort},
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        ) as stream:
            async for text in stream.text_stream:
                yield text
            final = await stream.get_final_message()
        if final.stop_reason == "refusal":
            yield " Sorry, I can't help with that one."


class EchoLLM:
    """Offline stand-in so the full audio loop can be demoed without an API key."""

    name = "echo"

    async def stream(self, history: list[dict]) -> AsyncIterator[str]:
        last = history[-1]["content"]
        reply = (f"You said: {last}. I'm running in offline echo mode, so set an Anthropic API "
                 "key to talk to Claude. Everything else in the pipeline is live.")
        for word in reply.split(" "):
            await asyncio.sleep(0.02)
            yield word + " "


# ---- Speech-to-text -----------------------------------------------------------------

@dataclass
class TranscriptEvent:
    text: str
    final: bool          # this segment won't change any more
    end_of_turn: bool    # the speaker paused: time to answer


class STTStream(Protocol):
    async def send(self, pcm16: bytes) -> None: ...
    def events(self) -> AsyncIterator[TranscriptEvent]: ...
    async def close(self) -> None: ...


class DeepgramSTT:
    """Deepgram live transcription over WebSocket (16 kHz mono PCM16 in, JSON results out)."""

    URL = "wss://api.deepgram.com/v1/listen"

    def __init__(self, api_key: str, sample_rate: int = 16_000, model: str = "nova-3"):
        self.api_key = api_key
        self.params = {
            "model": model, "encoding": "linear16", "sample_rate": sample_rate, "channels": 1,
            "interim_results": "true", "smart_format": "true", "endpointing": 300,
            "utterance_end_ms": 1000, "vad_events": "true",
        }
        self.ws = None
        self._keepalive: asyncio.Task | None = None

    async def connect(self) -> DeepgramSTT:
        from websockets.asyncio.client import connect

        self.ws = await connect(f"{self.URL}?{urlencode(self.params)}",
                                additional_headers={"Authorization": f"Token {self.api_key}"})
        self._keepalive = asyncio.create_task(self._keep_alive())
        return self

    async def _keep_alive(self) -> None:
        while True:  # Deepgram closes idle sockets after ~10 s without audio
            await asyncio.sleep(5)
            await self.ws.send(json.dumps({"type": "KeepAlive"}))

    async def send(self, pcm16: bytes) -> None:
        await self.ws.send(pcm16)

    async def events(self) -> AsyncIterator[TranscriptEvent]:
        finals: list[str] = []
        async for raw in self.ws:
            msg = json.loads(raw)
            if msg.get("type") == "UtteranceEnd" and finals:
                yield TranscriptEvent(" ".join(finals), final=True, end_of_turn=True)
                finals = []
                continue
            if msg.get("type") != "Results":
                continue
            text = msg["channel"]["alternatives"][0]["transcript"].strip()
            if not text:
                continue
            if msg.get("is_final"):
                finals.append(text)
                if msg.get("speech_final"):
                    yield TranscriptEvent(" ".join(finals), final=True, end_of_turn=True)
                    finals = []
                else:
                    yield TranscriptEvent(" ".join(finals), final=True, end_of_turn=False)
            else:
                yield TranscriptEvent(" ".join([*finals, text]), final=False, end_of_turn=False)

    async def close(self) -> None:
        if self._keepalive:
            self._keepalive.cancel()
        if self.ws:
            try:
                await self.ws.send(json.dumps({"type": "CloseStream"}))
            finally:
                await self.ws.close()


# ---- Text-to-speech -----------------------------------------------------------------

class TTS(Protocol):
    sample_rate: int

    async def synthesize(self, text: str) -> bytes: ...


class PollyTTS:
    """Amazon Polly neural voices, raw 16 kHz PCM16 (no decoding needed in the browser)."""

    sample_rate = 16_000

    def __init__(self, voice: str = "Joanna", region: str | None = None, client: Any | None = None):
        if client is None:
            import boto3

            client = boto3.client("polly", region_name=region)
        self.client = client
        self.voice = voice

    async def synthesize(self, text: str) -> bytes:
        def call() -> bytes:
            res = self.client.synthesize_speech(Text=text, OutputFormat="pcm", SampleRate=str(self.sample_rate),
                                                VoiceId=self.voice, Engine="neural")
            return res["AudioStream"].read()

        return await asyncio.to_thread(call)  # boto3 is blocking
