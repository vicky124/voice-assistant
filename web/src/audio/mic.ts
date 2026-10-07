import { ChunkBuffer, downsample, floatToPcm16 } from "./pcm";

export const TARGET_RATE = 16_000;

export interface MicHandle {
  stop(): void;
}

/** Capture the microphone and deliver 100 ms PCM16 / 16 kHz chunks. */
export async function startMic(onChunk: (pcm16: ArrayBuffer) => void): Promise<MicHandle> {
  const stream = await navigator.mediaDevices.getUserMedia({
    // Echo cancellation keeps the assistant's own voice out of the transcript (and out of barge-in).
    audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true },
  });
  const ctx = new AudioContext();
  await ctx.audioWorklet.addModule("/pcm-capture.worklet.js");
  const source = ctx.createMediaStreamSource(stream);
  const node = new AudioWorkletNode(ctx, "pcm-capture");
  const chunker = new ChunkBuffer(TARGET_RATE / 10);

  node.port.onmessage = (e: MessageEvent<Float32Array>) => {
    const resampled = downsample(e.data, ctx.sampleRate, TARGET_RATE);
    for (const chunk of chunker.push(resampled)) onChunk(floatToPcm16(chunk).buffer as ArrayBuffer);
  };
  source.connect(node);

  return {
    stop() {
      node.port.onmessage = null;
      source.disconnect();
      node.disconnect();
      stream.getTracks().forEach((t) => t.stop());
      void ctx.close();
    },
  };
}
