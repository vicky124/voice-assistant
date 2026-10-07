import { useCallback, useEffect, useRef, useState } from "react";
import { startMic, type MicHandle } from "./audio/mic";
import { isLikelyEcho, parseAudioFrame } from "./audio/pcm";
import { StreamingPlayer } from "./audio/player";
import { BrowserSpeaker, browserRecognitionAvailable, startRecognition, type RecognizerHandle } from "./speech/browser";

export type Status = "disconnected" | "connecting" | "listening" | "thinking" | "speaking";

export interface Message {
  id: number;
  role: "user" | "assistant";
  text: string;
  done: boolean;
  interrupted?: boolean;
}

export interface Capabilities {
  llm: string;
  server_stt: boolean;
  server_tts: boolean;
  tts_sample_rate: number | null;
}

export type Metrics = Record<string, number>;

type ServerEvent =
  | ({ type: "ready" } & Capabilities)
  | { type: "transcript"; text: string; final: boolean }
  | { type: "assistant_delta"; text: string }
  | { type: "assistant_sentence"; index: number; text: string }
  | { type: "turn_end"; metrics: Metrics; llm: string }
  | { type: "interrupted" }
  | { type: "error"; message: string };

function wsUrl(): string {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  return `${proto}://${location.host}/ws`;
}

export function useVoiceSession() {
  const [status, setStatus] = useState<Status>("disconnected");
  const [messages, setMessages] = useState<Message[]>([]);
  const [metrics, setMetrics] = useState<Metrics | null>(null);
  const [caps, setCaps] = useState<Capabilities | null>(null);
  const [error, setError] = useState<string | null>(null);

  const ws = useRef<WebSocket | null>(null);
  const capsRef = useRef<Capabilities | null>(null);
  const mic = useRef<MicHandle | null>(null);
  const recognizer = useRef<RecognizerHandle | null>(null);
  const player = useRef(new StreamingPlayer());
  const speaker = useRef(new BrowserSpeaker());
  const nextId = useRef(1);
  const openUser = useRef<number | null>(null);
  const openAssistant = useRef<number | null>(null);
  const turnStart = useRef(0);
  const firstAudio = useRef<number | null>(null);
  const statusRef = useRef<Status>("disconnected");

  const setStatusBoth = (s: Status) => {
    statusRef.current = s;
    setStatus(s);
  };

  const upsert = useCallback((which: typeof openUser, role: Message["role"], update: (m: Message) => Message) => {
    setMessages((prev) => {
      if (which.current === null) {
        const id = nextId.current++;
        which.current = id;
        return [...prev, update({ id, role, text: "", done: false })];
      }
      return prev.map((m) => (m.id === which.current ? update(m) : m));
    });
  }, []);

  const closeAssistant = useCallback((patch: Partial<Message>) => {
    const id = openAssistant.current;
    openAssistant.current = null;
    if (id !== null) setMessages((prev) => prev.map((m) => (m.id === id ? { ...m, ...patch, done: true } : m)));
  }, []);

  const send = (data: string | ArrayBuffer) => {
    if (ws.current?.readyState === WebSocket.OPEN) ws.current.send(data);
  };

  const outputPlaying = () => player.current.playing || speaker.current.playing;

  const interrupt = useCallback(() => {
    const played = Math.max(player.current.playedIndex, speaker.current.playedIndex);
    player.current.stop();
    speaker.current.stop();
    send(JSON.stringify({ type: "interrupt", played_index: played >= 0 ? played : null }));
  }, []);

  const markAudioStarted = () => {
    if (firstAudio.current === null && turnStart.current) {
      firstAudio.current = Math.round(performance.now() - turnStart.current);
    }
    setStatusBoth("speaking");
  };

  const startInput = useCallback(async (c: Capabilities) => {
    if (c.server_stt) {
      mic.current = await startMic((pcm) => send(pcm));
      return;
    }
    if (!browserRecognitionAvailable()) {
      setError("No server STT configured and this browser lacks speech recognition. Use the text box.");
      return;
    }
    recognizer.current = startRecognition({
      onInterim: (text) => {
        if (outputPlaying() && isLikelyEcho(text, speaker.current.current)) return;
        if (outputPlaying() && text.split(/\s+/).length >= 2) interrupt(); // barge-in
        openAssistant.current = null;
        upsert(openUser, "user", (m) => ({ ...m, text }));
      },
      onFinal: (text) => {
        if (outputPlaying() && isLikelyEcho(text, speaker.current.current)) return;
        turnStart.current = performance.now();
        send(JSON.stringify({ type: "user_text", text }));
      },
      onError: (e) => setError(`speech recognition: ${e}`),
    });
  }, [interrupt, upsert]);

  const handleEvent = useCallback((ev: ServerEvent) => {
    switch (ev.type) {
      case "ready":
        capsRef.current = ev;
        setCaps(ev);
        setStatusBoth("listening");
        void startInput(ev).catch((e: Error) => setError(e.message));
        break;
      case "transcript":
        if (ev.final && capsRef.current?.server_stt) turnStart.current = performance.now();
        upsert(openUser, "user", (m) => ({ ...m, text: ev.text, done: ev.final }));
        break;
      case "assistant_delta":
        if (openAssistant.current === null) {
          openUser.current = null;
          firstAudio.current = null;
          player.current.resetTurn();
          speaker.current.resetTurn();
          if (statusRef.current !== "speaking") setStatusBoth("thinking");
        }
        upsert(openAssistant, "assistant", (m) => ({ ...m, text: m.text + ev.text }));
        break;
      case "assistant_sentence":
        if (!capsRef.current?.server_tts) {
          speaker.current.speak(ev.index, ev.text);
          markAudioStarted();
        }
        break;
      case "turn_end":
        setMetrics({ ...ev.metrics, ...(firstAudio.current !== null ? { client_first_audio_ms: firstAudio.current } : {}) });
        closeAssistant({});
        if (!outputPlaying()) setStatusBoth("listening");
        break;
      case "interrupted":
        player.current.stop();
        speaker.current.stop();
        closeAssistant({ interrupted: true });
        setStatusBoth("listening");
        break;
      case "error":
        setError(ev.message);
        closeAssistant({ interrupted: true });
        setStatusBoth("listening");
        break;
    }
  }, [closeAssistant, startInput, upsert]);

  const connect = useCallback(() => {
    setError(null);
    setStatusBoth("connecting");
    const socket = new WebSocket(wsUrl());
    socket.binaryType = "arraybuffer";
    socket.onmessage = (e) => {
      if (typeof e.data === "string") {
        handleEvent(JSON.parse(e.data) as ServerEvent);
      } else {
        const { index, samples } = parseAudioFrame(e.data as ArrayBuffer);
        player.current.enqueue(index, samples, capsRef.current?.tts_sample_rate ?? 16_000);
        markAudioStarted();
      }
    };
    socket.onclose = () => setStatusBoth("disconnected");
    socket.onerror = () => setError("Connection failed. Is the server running on :8000?");
    ws.current = socket;
  }, [handleEvent]);

  const disconnect = useCallback(() => {
    mic.current?.stop();
    recognizer.current?.stop();
    mic.current = recognizer.current = null;
    player.current.stop();
    speaker.current.stop();
    ws.current?.close();
    ws.current = null;
    openUser.current = openAssistant.current = null;
  }, []);

  const sendText = useCallback((text: string) => {
    if (!text.trim()) return;
    if (outputPlaying()) interrupt();
    turnStart.current = performance.now();
    openUser.current = null;
    send(JSON.stringify({ type: "user_text", text }));
  }, [interrupt]);

  useEffect(() => {
    const onIdle = () => {
      if (openAssistant.current === null && statusRef.current === "speaking") setStatusBoth("listening");
    };
    player.current.onIdle = onIdle;
    speaker.current.onIdle = onIdle;
    return disconnect;
  }, [disconnect]);

  return { status, messages, metrics, caps, error, connect, disconnect, interrupt, sendText };
}
