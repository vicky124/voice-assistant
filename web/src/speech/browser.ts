/** Browser-native speech fallbacks (Web Speech API) used when the server has no STT/TTS keys. */

interface RecognitionResultEvent {
  resultIndex: number;
  results: ArrayLike<{ isFinal: boolean; 0: { transcript: string } }>;
}

interface Recognition {
  continuous: boolean;
  interimResults: boolean;
  lang: string;
  onresult: ((e: RecognitionResultEvent) => void) | null;
  onend: (() => void) | null;
  onerror: ((e: { error: string }) => void) | null;
  start(): void;
  stop(): void;
}

type RecognitionCtor = new () => Recognition;

export function browserRecognitionAvailable(): boolean {
  const w = window as unknown as { SpeechRecognition?: RecognitionCtor; webkitSpeechRecognition?: RecognitionCtor };
  return Boolean(w.SpeechRecognition ?? w.webkitSpeechRecognition);
}

export interface RecognizerHandle {
  stop(): void;
}

export function startRecognition(handlers: {
  onInterim: (text: string) => void;
  onFinal: (text: string) => void;
  onError: (message: string) => void;
}): RecognizerHandle {
  const w = window as unknown as { SpeechRecognition?: RecognitionCtor; webkitSpeechRecognition?: RecognitionCtor };
  const Ctor = w.SpeechRecognition ?? w.webkitSpeechRecognition;
  if (!Ctor) throw new Error("Speech recognition is not supported in this browser");
  const rec = new Ctor();
  rec.continuous = true;
  rec.interimResults = true;
  rec.lang = navigator.language || "en-US";
  let stopped = false;

  rec.onresult = (e) => {
    for (let i = e.resultIndex; i < e.results.length; i++) {
      const text = e.results[i][0].transcript.trim();
      if (!text) continue;
      if (e.results[i].isFinal) handlers.onFinal(text);
      else handlers.onInterim(text);
    }
  };
  rec.onerror = (e) => {
    if (e.error !== "no-speech" && e.error !== "aborted") handlers.onError(e.error);
  };
  rec.onend = () => {
    if (!stopped) rec.start(); // Chrome ends sessions after silence; keep listening
  };
  rec.start();
  return {
    stop() {
      stopped = true;
      rec.stop();
    },
  };
}

/** Speaks sentences in order with speechSynthesis and reports which ones finished. */
export class BrowserSpeaker {
  playedIndex = -1;
  current = "";
  private pending = 0;
  onIdle?: () => void;

  get playing(): boolean {
    return this.pending > 0;
  }

  speak(index: number, text: string): void {
    const u = new SpeechSynthesisUtterance(text);
    this.pending++;
    u.onstart = () => {
      this.current = text;
    };
    u.onend = () => {
      this.pending = Math.max(0, this.pending - 1);
      this.playedIndex = Math.max(this.playedIndex, index);
      if (!this.playing) {
        this.current = "";
        this.onIdle?.();
      }
    };
    speechSynthesis.speak(u);
  }

  stop(): void {
    this.pending = 0;
    this.current = "";
    speechSynthesis.cancel();
  }

  resetTurn(): void {
    this.playedIndex = -1;
  }
}
