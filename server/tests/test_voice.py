import asyncio
import json
import struct
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from voice.app import Providers, create_app
from voice.chunker import SentenceChunker
from voice.providers import ClaudeLLM, TranscriptEvent
from voice.session import VoiceSession

# ---- sentence chunking ----------------------------------------------------------------


def chunk_all(text: str, step: int = 3, **kw) -> list[str]:
    c = SentenceChunker(**kw)
    out = []
    for i in range(0, len(text), step):  # feed in small, token-like pieces
        out += c.push(text[i:i + step])
    return out + c.flush()


def test_splits_on_sentence_boundaries_while_streaming():
    assert chunk_all("The weather is sunny today. Expect highs of 25 degrees! Need anything else?") == [
        "The weather is sunny today.", "Expect highs of 25 degrees!", "Need anything else?"]


def test_does_not_split_abbreviations_decimals_or_tiny_sentences():
    out = chunk_all("Yes. Dr. Smith measured 3.14 meters today. That is all for now.")
    assert out == ["Yes. Dr. Smith measured 3.14 meters today.", "That is all for now."]


def test_long_clause_breaks_at_comma():
    text = "When you get to the station, " * 6 + "turn left."
    out = chunk_all(text, soft_limit=60)
    assert len(out) > 1 and all(len(s) <= 100 for s in out[:-1])


def test_strips_markdown_for_speech():
    assert chunk_all("**Sure** thing, here is `code` now.") == ["Sure thing, here is code now."]


# ---- session ---------------------------------------------------------------------------


class ScriptedLLM:
    name = "scripted"

    def __init__(self, text: str, delay: float = 0.0):
        self.text = text
        self.delay = delay
        self.histories: list[list[dict]] = []

    async def stream(self, history):
        self.histories.append(history)
        for word in self.text.split(" "):
            await asyncio.sleep(self.delay)
            yield word + " "


class FakeTTS:
    sample_rate = 16_000

    def __init__(self, delay: float = 0.0):
        self.delay = delay
        self.spoken: list[str] = []

    async def synthesize(self, text: str) -> bytes:
        await asyncio.sleep(self.delay)
        self.spoken.append(text)
        return b"\x01\x00" * 40_000  # 80 KB -> split into 3 frames


class Recorder:
    def __init__(self):
        self.json: list[dict] = []
        self.audio: list[bytes] = []

    async def send_json(self, m):
        self.json.append(m)

    async def send_bytes(self, b):
        self.audio.append(b)

    def types(self):
        return [m["type"] for m in self.json]


REPLY = "Paris is the capital of France. It has about two million residents. Anything else?"


async def wait_done(session):
    await asyncio.wait_for(session._task, 2)


async def test_turn_streams_sentences_audio_and_metrics():
    rec, tts = Recorder(), FakeTTS()
    session = VoiceSession(rec.send_json, rec.send_bytes, ScriptedLLM(REPLY), tts)
    await session.user_said("What's the capital of France?")
    await wait_done(session)

    sentences = [m for m in rec.json if m["type"] == "assistant_sentence"]
    assert [s["index"] for s in sentences] == [0, 1, 2]
    assert tts.spoken == [s["text"] for s in sentences]
    indexes = [struct.unpack(">I", frame[:4])[0] for frame in rec.audio]
    assert indexes == sorted(indexes) and indexes.count(0) == 3  # in order, chunked frames
    metrics = rec.json[-1]["metrics"]
    assert rec.json[-1]["type"] == "turn_end"
    assert {"llm_first_token_ms", "first_sentence_ms", "first_audio_ms", "total_ms"} <= set(metrics)
    assert metrics["llm_first_token_ms"] <= metrics["first_sentence_ms"] <= metrics["first_audio_ms"]
    assert session.history[-1] == {"role": "assistant", "content": REPLY}


async def test_barge_in_cancels_reply_and_records_only_what_was_heard():
    rec, tts = Recorder(), FakeTTS()
    llm = ScriptedLLM(REPLY + " " + "More words follow here. " * 20, delay=0.01)
    session = VoiceSession(rec.send_json, rec.send_bytes, llm, tts)
    await session.user_said("Tell me about Paris")
    while len([m for m in rec.json if m["type"] == "assistant_sentence"]) < 2:
        await asyncio.sleep(0.005)
    await session.interrupt(played_index=0)  # client finished playing sentence 0 only

    assert rec.types()[-1] == "interrupted" and "turn_end" not in rec.types()
    assert session.history[-1] == {"role": "assistant",
                                   "content": "Paris is the capital of France. [interrupted by user]"}
    await session.user_said("Actually, what about Rome?")
    await wait_done(session)
    roles = [m["role"] for m in llm.histories[-1]]
    assert roles == ["user", "assistant", "user"]


async def test_interrupt_before_any_speech_leaves_consecutive_user_turns():
    rec = Recorder()
    llm = ScriptedLLM(REPLY, delay=0.05)
    session = VoiceSession(rec.send_json, rec.send_bytes, llm)
    await session.user_said("first question")
    await asyncio.sleep(0.01)
    await session.user_said("never mind, second question")  # implicit barge-in
    await wait_done(session)
    assert [m["role"] for m in llm.histories[-1]] == ["user", "user"]


async def test_llm_failure_reports_error_and_session_survives():
    class Broken:
        name = "broken"

        async def stream(self, history):
            raise RuntimeError("upstream down")
            yield  # pragma: no cover

    rec = Recorder()
    session = VoiceSession(rec.send_json, rec.send_bytes, Broken())
    await session.user_said("hello?")
    await wait_done(session)
    assert rec.json[-1]["type"] == "error" and session.history == []


async def test_stt_speech_during_reply_triggers_barge_in_then_new_turn():
    class FakeSTT:
        async def events(self):
            await asyncio.sleep(0.05)
            yield TranscriptEvent("wait stop", final=False, end_of_turn=False)
            yield TranscriptEvent("wait stop please", final=True, end_of_turn=True)

    rec = Recorder()
    llm = ScriptedLLM("Long answer. " * 50, delay=0.005)
    session = VoiceSession(rec.send_json, rec.send_bytes, llm)
    await session.user_said("Explain everything")
    await session.pump_stt(FakeSTT())
    await wait_done(session)
    assert "interrupted" in rec.types()
    assert llm.histories[-1][-1] == {"role": "user", "content": "wait stop please"}


# ---- websocket ---------------------------------------------------------------------------


def test_websocket_browser_mode_round_trip():
    app = create_app(Providers(llm=ScriptedLLM("Hello there, nice to meet you. Bye now."), tts=None,
                               stt_factory=None))
    with TestClient(app).websocket_connect("/ws") as ws:
        ready = json.loads(ws.receive_text())
        assert ready == {"type": "ready", "llm": "scripted", "server_stt": False, "server_tts": False,
                         "tts_sample_rate": None}
        ws.send_text(json.dumps({"type": "user_text", "text": "hi"}))
        seen = []
        while not seen or seen[-1]["type"] != "turn_end":
            seen.append(json.loads(ws.receive_text()))
    sentences = [m["text"] for m in seen if m["type"] == "assistant_sentence"]
    assert sentences == ["Hello there, nice to meet you.", "Bye now."]
    assert seen[0] == {"type": "transcript", "text": "hi", "final": True}


# ---- Claude adapter ------------------------------------------------------------------------


async def test_claude_llm_request_and_refusal_fallback_text():
    captured = {}

    class Stream:
        def __init__(self, stop):
            self.stop = stop

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        @property
        async def text_stream(self):
            for t in ["Hi", " there."]:
                yield t

        async def get_final_message(self):
            return SimpleNamespace(stop_reason=self.stop)

    state = {"stop": "end_turn"}

    def stream(**kwargs):
        captured.update(kwargs)
        return Stream(state["stop"])

    client = SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(stream=stream)))
    llm = ClaudeLLM(client=client)
    assert "".join([t async for t in llm.stream([{"role": "user", "content": "hey"}])]) == "Hi there."
    assert captured["model"] == "claude-opus-5-5"
    assert captured["output_config"] == {"effort": "low"}
    assert captured["fallbacks"] == "default"
    assert "no markdown" in captured["system"]
    state["stop"] = "refusal"
    text = "".join([t async for t in llm.stream([{"role": "user", "content": "hey"}])])
    assert text.endswith("I can't help with that one.")


@pytest.fixture(autouse=True)
def _no_real_keys(monkeypatch):
    for k in ("ANTHROPIC_API_KEY", "DEEPGRAM_API_KEY", "TTS_PROVIDER"):
        monkeypatch.delenv(k, raising=False)


async def test_deepgram_result_parsing():
    from voice.providers import DeepgramSTT

    def result(text, is_final=False, speech_final=False):
        return json.dumps({"type": "Results", "is_final": is_final, "speech_final": speech_final,
                           "channel": {"alternatives": [{"transcript": text}]}})

    class FakeWS:
        def __aiter__(self):
            async def gen():
                for m in [result("what's the"), result("what's the weather", True),
                          result("in paris"), result("in paris", True, True),
                          result(""), result("thanks", True), json.dumps({"type": "UtteranceEnd"})]:
                    yield m
            return gen()

    stt = DeepgramSTT("key")
    stt.ws = FakeWS()
    events = [(e.text, e.final, e.end_of_turn) async for e in stt.events()]
    assert events == [
        ("what's the", False, False),
        ("what's the weather", True, False),
        ("what's the weather in paris", False, False),
        ("what's the weather in paris", True, True),
        ("thanks", True, False),
        ("thanks", True, True),  # UtteranceEnd closes a turn that had no speech_final
    ]
