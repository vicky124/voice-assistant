import { describe, expect, it } from "vitest";
import { ChunkBuffer, downsample, floatToPcm16, isLikelyEcho, parseAudioFrame, pcm16ToFloat } from "./pcm";

describe("downsample", () => {
  it("averages windows from 48 kHz to 16 kHz", () => {
    const input = Float32Array.from([0, 0.3, 0.6, 1, 1, 1]);
    expect(Array.from(downsample(input, 48_000, 16_000))).toEqual([expect.closeTo(0.3), 1]);
  });

  it("passes through at the same rate and rejects upsampling", () => {
    const x = new Float32Array([0.1]);
    expect(downsample(x, 16_000, 16_000)).toBe(x);
    expect(() => downsample(x, 8_000, 16_000)).toThrow();
  });
});

describe("PCM16", () => {
  it("round-trips and clips", () => {
    const pcm = floatToPcm16(Float32Array.from([-1, -0.5, 0, 0.5, 1, 2]));
    expect(Array.from(pcm)).toEqual([-32768, -16384, 0, 16383, 32767, 32767]);
    const back = pcm16ToFloat(pcm);
    expect(back[0]).toBe(-1);
    expect(back[4]).toBe(1);
  });

  it("parses server frames: big-endian index + little-endian samples", () => {
    const buf = new ArrayBuffer(8);
    const view = new DataView(buf);
    view.setUint32(0, 7, false);
    view.setInt16(4, 32767, true);
    view.setInt16(6, -32768, true);
    const { index, samples } = parseAudioFrame(buf);
    expect(index).toBe(7);
    expect(Array.from(samples)).toEqual([1, -1]);
  });
});

describe("ChunkBuffer", () => {
  it("emits fixed-size chunks and keeps the remainder", () => {
    const b = new ChunkBuffer(4);
    expect(b.push(new Float32Array(3))).toHaveLength(0);
    const out = b.push(Float32Array.from([1, 2, 3, 4, 5, 6]));
    expect(out.map((c) => c.length)).toEqual([4, 4]);
    expect(Array.from(out[1])).toEqual([2, 3, 4, 5]);
    expect(b.push(Float32Array.from([7, 8, 9]))).toHaveLength(1);
  });
});

describe("isLikelyEcho", () => {
  it("flags the assistant's own words but not a real interruption", () => {
    const spoken = "Paris is the capital of France.";
    expect(isLikelyEcho("capital of France", spoken)).toBe(true);
    expect(isLikelyEcho("wait stop that", spoken)).toBe(false);
  });
});
