"""One voice conversation: turn-taking, streaming, barge-in, and latency metrics.

Wire protocol (JSON text frames unless noted):

  client -> server   {"type": "user_text", "text": ...}           browser-STT mode
                     binary PCM16 16 kHz mono frames                server-STT mode
                     {"type": "interrupt", "played_index": n}      user barged in
  server -> client   {"type": "transcript", "text", "final"}
                     {"type": "assistant_delta", "text"}           raw tokens (captions)
                     {"type": "assistant_sentence", "index", "text"}  unit of speech
                     binary: 4-byte big-endian sentence index + PCM16 audio
                     {"type": "turn_end", "metrics": {...}} / {"type": "interrupted"}
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import struct
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from voice.chunker import SentenceChunker
from voice.providers import LLM, TTS, STTStream

log = logging.getLogger(__name__)

SendJSON = Callable[[dict], Awaitable[None]]
SendBytes = Callable[[bytes], Awaitable[None]]
AUDIO_FRAME = 32_768  # bytes of PCM per WebSocket frame (~1 s at 16 kHz)
MAX_HISTORY = 40      # messages kept; older turns fall off


@dataclass
class Turn:
    started: float
    sentences: list[str] = field(default_factory=list)
    metrics: dict[str, float] = field(default_factory=dict)

    def mark(self, name: str, now: float) -> None:
        self.metrics.setdefault(name, round((now - self.started) * 1000, 1))


class VoiceSession:
    def __init__(self, send_json: SendJSON, send_bytes: SendBytes, llm: LLM, tts: TTS | None = None,
                 clock: Callable[[], float] = time.perf_counter):
        self.send_json = send_json
        self.send_bytes = send_bytes
        self.llm = llm
        self.tts = tts
        self.clock = clock
        self.history: list[dict] = []
        self.turn: Turn | None = None
        self._task: asyncio.Task | None = None
        self._played_index: int | None = None

    @property
    def speaking(self) -> bool:
        return self._task is not None and not self._task.done()

    # ---- inputs ---------------------------------------------------------------

    async def user_said(self, text: str) -> None:
        text = text.strip()
        if not text:
            return
        await self.interrupt()
        self.turn = Turn(started=self.clock())
        self._task = asyncio.create_task(self._run_turn(text, self.turn))

    async def interrupt(self, played_index: int | None = None) -> None:
        """Stop the current reply (barge-in). `played_index` is the last sentence the client
        finished playing, so history records what the user actually heard."""
        if not self.speaking:
            return
        self._played_index = played_index
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
        await self.send_json({"type": "interrupted"})

    async def pump_stt(self, stt: STTStream) -> None:
        """Forward transcripts; speech while the assistant is talking is a barge-in."""
        async for event in stt.events():
            await self.send_json({"type": "transcript", "text": event.text, "final": event.final})
            if self.speaking and len(event.text.split()) >= 2:  # ignore one-word noise/echo blips
                await self.interrupt()
            if event.end_of_turn:
                await self.user_said(event.text)

    async def close(self) -> None:
        if self.speaking:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    # ---- one assistant turn -----------------------------------------------------

    async def _run_turn(self, text: str, turn: Turn) -> None:
        self.history.append({"role": "user", "content": text})
        del self.history[:-MAX_HISTORY]
        chunker = SentenceChunker()
        queue: asyncio.Queue[tuple[int, str] | None] = asyncio.Queue()
        tts_task = asyncio.create_task(self._speak(queue, turn)) if self.tts else None
        full = []
        answered = False

        async def emit(sentence: str) -> None:
            index = len(turn.sentences)
            turn.sentences.append(sentence)
            turn.mark("first_sentence_ms", self.clock())
            await self.send_json({"type": "assistant_sentence", "index": index, "text": sentence})
            if tts_task:
                await queue.put((index, sentence))

        try:
            async for delta in self.llm.stream(list(self.history)):
                turn.mark("llm_first_token_ms", self.clock())
                full.append(delta)
                await self.send_json({"type": "assistant_delta", "text": delta})
                for sentence in chunker.push(delta):
                    await emit(sentence)
            for sentence in chunker.flush():
                await emit(sentence)
            if tts_task:
                await queue.put(None)
                await tts_task
            self.history.append({"role": "assistant", "content": "".join(full).strip()})
            answered = True
            turn.mark("total_ms", self.clock())
            await self.send_json({"type": "turn_end", "metrics": turn.metrics, "llm": self.llm.name})
        except asyncio.CancelledError:
            if tts_task:
                tts_task.cancel()
            if not answered:
                self._record_interrupted(turn)
            raise
        except Exception as exc:  # provider failure: tell the client, keep the session alive
            log.exception("turn failed")
            if tts_task:
                tts_task.cancel()
            self.history.pop()  # drop the unanswered user message
            await self.send_json({"type": "error", "message": f"assistant unavailable: {type(exc).__name__}"})

    def _record_interrupted(self, turn: Turn) -> None:
        heard = turn.sentences if self._played_index is None else turn.sentences[: self._played_index + 1]
        if heard:
            # Tell the model where it was cut off so the next reply doesn't repeat itself.
            self.history.append({"role": "assistant", "content": " ".join(heard) + " [interrupted by user]"})
        # If nothing was heard, the user message stays; the next one is merged with it
        # (consecutive user turns are allowed and combined by the API).
        self._played_index = None

    async def _speak(self, queue: asyncio.Queue, turn: Turn) -> None:
        # Sentences are synthesized in order; sentence N+1 is synthesized while N plays.
        while (item := await queue.get()) is not None:
            index, sentence = item
            audio = await self.tts.synthesize(sentence)
            turn.mark("first_audio_ms", self.clock())
            header = struct.pack(">I", index)
            for start in range(0, len(audio), AUDIO_FRAME):
                await self.send_bytes(header + audio[start:start + AUDIO_FRAME])
