// Runs on the audio rendering thread: forwards raw mono Float32 frames to the main thread.
// Downsampling and PCM16 encoding happen on the main thread, keeping this processor tiny.
class PcmCaptureProcessor extends AudioWorkletProcessor {
  process(inputs) {
    const channel = inputs[0] && inputs[0][0];
    if (channel) this.port.postMessage(channel.slice(0));
    return true;
  }
}
registerProcessor("pcm-capture", PcmCaptureProcessor);
