/** Pure audio helpers: resampling, PCM16 encoding, and the server's audio frame format. */

/** Downsample by averaging each output sample's source window (a cheap low-pass filter). */
export function downsample(input: Float32Array, inRate: number, outRate: number): Float32Array {
  if (outRate === inRate) return input;
  if (outRate > inRate) throw new Error("upsampling is not supported");
  const ratio = inRate / outRate;
  const out = new Float32Array(Math.floor(input.length / ratio));
  for (let i = 0; i < out.length; i++) {
    const start = Math.floor(i * ratio);
    const end = Math.min(input.length, Math.floor((i + 1) * ratio));
    let sum = 0;
    for (let j = start; j < end; j++) sum += input[j];
    out[i] = sum / Math.max(1, end - start);
  }
  return out;
}

export function floatToPcm16(input: Float32Array): Int16Array {
  const out = new Int16Array(input.length);
  for (let i = 0; i < input.length; i++) {
    const s = Math.max(-1, Math.min(1, input[i]));
    out[i] = s < 0 ? s * 0x8000 : s * 0x7fff;
  }
  return out;
}

export function pcm16ToFloat(input: Int16Array): Float32Array {
  const out = new Float32Array(input.length);
  for (let i = 0; i < input.length; i++) out[i] = input[i] / (input[i] < 0 ? 0x8000 : 0x7fff);
  return out;
}

/** Server audio frame: 4-byte big-endian sentence index, then little-endian PCM16 samples. */
export function parseAudioFrame(buffer: ArrayBuffer): { index: number; samples: Float32Array } {
  const view = new DataView(buffer);
  const index = view.getUint32(0, false);
  const count = (buffer.byteLength - 4) >> 1;
  const pcm = new Int16Array(count);
  for (let i = 0; i < count; i++) pcm[i] = view.getInt16(4 + i * 2, true);
  return { index, samples: pcm16ToFloat(pcm) };
}

/** Accumulates small worklet frames into fixed-size chunks (fewer, larger WebSocket sends). */
export class ChunkBuffer {
  private parts: Float32Array[] = [];
  private length = 0;

  constructor(private readonly chunkSize: number) {}

  push(frame: Float32Array): Float32Array[] {
    this.parts.push(frame);
    this.length += frame.length;
    const out: Float32Array[] = [];
    while (this.length >= this.chunkSize) {
      const merged = new Float32Array(this.length);
      let offset = 0;
      for (const p of this.parts) {
        merged.set(p, offset);
        offset += p.length;
      }
      out.push(merged.subarray(0, this.chunkSize));
      const rest = merged.slice(this.chunkSize);
      this.parts = rest.length ? [rest] : [];
      this.length = rest.length;
    }
    return out;
  }
}

/** Crude echo filter for browser speech recognition: is `heard` mostly the assistant's own words? */
export function isLikelyEcho(heard: string, spoken: string): boolean {
  const words = (s: string) => s.toLowerCase().replace(/[^a-z0-9' ]/g, " ").split(/\s+/).filter(Boolean);
  const h = words(heard);
  if (h.length === 0) return true;
  const s = new Set(words(spoken));
  return h.filter((w) => s.has(w)).length / h.length >= 0.6;
}
