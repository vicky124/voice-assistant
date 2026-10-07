import { useEffect, useRef, useState, type FormEvent } from "react";
import { useVoiceSession, type Metrics, type Status } from "./useVoiceSession";

const STATUS_LABEL: Record<Status, string> = {
  disconnected: "Tap to start",
  connecting: "Connecting…",
  listening: "Listening",
  thinking: "Thinking…",
  speaking: "Speaking (talk to interrupt)",
};

const METRIC_LABELS: [keyof Metrics, string][] = [
  ["llm_first_token_ms", "First token"],
  ["first_sentence_ms", "First sentence"],
  ["first_audio_ms", "First audio (server)"],
  ["client_first_audio_ms", "First audio (heard)"],
  ["total_ms", "Turn total"],
];

export function App() {
  const { status, messages, metrics, caps, error, connect, disconnect, interrupt, sendText } = useVoiceSession();
  const [draft, setDraft] = useState("");
  const logRef = useRef<HTMLDivElement>(null);
  const live = status !== "disconnected";

  useEffect(() => {
    logRef.current?.scrollTo({ top: logRef.current.scrollHeight, behavior: "smooth" });
  }, [messages]);

  const submit = (e: FormEvent) => {
    e.preventDefault();
    sendText(draft);
    setDraft("");
  };

  return (
    <main className="app">
      <header>
        <h1>Voice Assistant</h1>
        {caps && (
          <div className="badges" aria-label="Pipeline configuration">
            <span>LLM · {caps.llm}</span>
            <span>STT · {caps.server_stt ? "Deepgram (server)" : "browser"}</span>
            <span>TTS · {caps.server_tts ? "Polly (server)" : "browser"}</span>
          </div>
        )}
      </header>

      <section className="stage">
        <button
          className={`orb ${status}`}
          onClick={live ? (status === "speaking" ? interrupt : disconnect) : connect}
          aria-label={live ? (status === "speaking" ? "Interrupt" : "Stop session") : "Start session"}
        >
          <span className="ring" />
        </button>
        <p className="status" role="status">{STATUS_LABEL[status]}</p>
        {live && <button className="link" onClick={disconnect}>End session</button>}
        {error && <p className="error" role="alert">{error}</p>}
      </section>

      <section className="log" ref={logRef} aria-live="polite">
        {messages.length === 0 && <p className="empty">Say something like “What's a good name for a cat?”</p>}
        {messages.map((m) => (
          <div key={m.id} className={`bubble ${m.role} ${m.done ? "" : "pending"}`}>
            {m.text || "…"}
            {m.interrupted && <span className="tag">interrupted</span>}
          </div>
        ))}
      </section>

      <form className="composer" onSubmit={submit}>
        <input value={draft} onChange={(e) => setDraft(e.target.value)} placeholder={live ? "Or type a message" : "Start a session first"}
          disabled={!live} aria-label="Message" />
        <button disabled={!live || !draft.trim()}>Send</button>
      </form>

      <section className="metrics" aria-label="Latency for the last turn">
        {METRIC_LABELS.map(([key, label]) => (
          <div key={key}>
            <span>{label}</span>
            <strong>{metrics?.[key] !== undefined ? `${Math.round(metrics[key])} ms` : "–"}</strong>
          </div>
        ))}
      </section>
    </main>
  );
}
