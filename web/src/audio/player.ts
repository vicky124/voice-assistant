/**
 * Gapless playback of streamed PCM chunks. Each chunk is scheduled to start exactly when the
 * previous one ends, so sentence audio arriving over the network plays seamlessly. Tracks
 * which sentence has *finished playing*, which the server uses to record what the user heard.
 */
export class StreamingPlayer {
  private ctx: AudioContext | null = null;
  private nextStart = 0;
  private sources = new Set<AudioBufferSourceNode>();
  playedIndex = -1;
  onIdle?: () => void;

  private context(): AudioContext {
    this.ctx ??= new AudioContext();
    if (this.ctx.state === "suspended") void this.ctx.resume();
    return this.ctx;
  }

  get playing(): boolean {
    return this.sources.size > 0;
  }

  enqueue(index: number, samples: Float32Array, sampleRate: number): void {
    const ctx = this.context();
    const buffer = ctx.createBuffer(1, samples.length, sampleRate);
    buffer.copyToChannel(samples as Float32Array<ArrayBuffer>, 0);
    const source = ctx.createBufferSource();
    source.buffer = buffer;
    source.connect(ctx.destination);
    const startAt = Math.max(ctx.currentTime + 0.02, this.nextStart);
    source.start(startAt);
    this.nextStart = startAt + buffer.duration;
    this.sources.add(source);
    source.onended = () => {
      this.sources.delete(source);
      this.playedIndex = Math.max(this.playedIndex, index);
      if (!this.playing) this.onIdle?.();
    };
  }

  /** Barge-in: silence immediately and drop everything queued. */
  stop(): void {
    for (const s of this.sources) {
      s.onended = null;
      try {
        s.stop();
      } catch {
        /* already stopped */
      }
    }
    this.sources.clear();
    this.nextStart = 0;
  }

  resetTurn(): void {
    this.playedIndex = -1;
  }
}
