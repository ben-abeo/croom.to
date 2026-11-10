package to.croom.doorsign;

import android.annotation.SuppressLint;
import android.media.AudioFormat;
import android.media.AudioRecord;
import android.media.MediaCodec;
import android.media.MediaCodecInfo;
import android.media.MediaFormat;
import android.media.MediaRecorder;
import java.nio.ByteBuffer;

/**
 * The panel's microphone encoded to AAC-LC (spec 2026-11-10): 16 kHz mono at 32 kbit/s, while the room's Door sign camera sound is on.
 * One thread of its own reads the microphone and feeds the encoder; a failure is reported once to the sink, which turns the sound off
 * and keeps the picture. Stopped, the thread ends within one read (64 ms).
 */
final class SoundStream {

    interface Sink {
        /** The encoder's AudioSpecificConfig (the SDP's config=), before the first frame. */
        void soundConfig(byte[] audioSpecificConfig);

        /** One raw AAC frame (no ADTS header). */
        void sound(byte[] frame, long presentationUs);

        void soundFailed(String why);
    }

    private static final int FRAME_BYTES = 2048;      // 1024 samples of 16-bit mono: one AAC frame

    private final Sink sink;
    private volatile boolean stopped;

    SoundStream(Sink sink) {
        this.sink = sink;
    }

    void start() {
        Thread thread = new Thread(this::run, "door-sign-sound");
        thread.setDaemon(true);
        thread.start();
    }

    void stop() {
        stopped = true;
    }

    @SuppressLint("MissingPermission")   // granted as device owner while the room records sound (Panel.grantPermissions)
    private void run() {
        AudioRecord mic = null;
        MediaCodec codec = null;
        try {
            int min = AudioRecord.getMinBufferSize(Onvif.SAMPLE_RATE, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT);
            if (min <= 0) throw new IllegalStateException("the microphone has no " + Onvif.SAMPLE_RATE / 1000 + " kHz mono");
            mic = new AudioRecord(MediaRecorder.AudioSource.MIC, Onvif.SAMPLE_RATE, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT,
                    Math.max(min, FRAME_BYTES) * 4);
            if (mic.getState() != AudioRecord.STATE_INITIALIZED) throw new IllegalStateException("the microphone would not open");
            MediaFormat format = MediaFormat.createAudioFormat(MediaFormat.MIMETYPE_AUDIO_AAC, Onvif.SAMPLE_RATE, 1);
            format.setInteger(MediaFormat.KEY_AAC_PROFILE, MediaCodecInfo.CodecProfileLevel.AACObjectLC);
            format.setInteger(MediaFormat.KEY_BIT_RATE, Onvif.SOUND_BITRATE_KBPS * 1000);
            format.setInteger(MediaFormat.KEY_MAX_INPUT_SIZE, FRAME_BYTES);
            codec = MediaCodec.createEncoderByType(MediaFormat.MIMETYPE_AUDIO_AAC);
            codec.configure(format, null, null, MediaCodec.CONFIGURE_FLAG_ENCODE);
            codec.start();
            mic.startRecording();
            byte[] pcm = new byte[FRAME_BYTES];
            long samples = 0;
            MediaCodec.BufferInfo info = new MediaCodec.BufferInfo();
            while (!stopped) {
                int in = codec.dequeueInputBuffer(10_000);
                if (in >= 0) {
                    ByteBuffer buffer = codec.getInputBuffer(in);
                    int read = mic.read(pcm, 0, Math.min(pcm.length, buffer.remaining()));
                    if (read < 0) throw new IllegalStateException("the microphone stopped (" + read + ")");
                    buffer.put(pcm, 0, read);
                    codec.queueInputBuffer(in, 0, read, samples * 1_000_000 / Onvif.SAMPLE_RATE, 0);
                    samples += read / 2;
                }
                drain(codec, info);
            }
        } catch (Exception e) {
            if (!stopped) sink.soundFailed(String.valueOf(e.getMessage() == null ? e : e.getMessage()));
        } finally {
            try {
                if (mic != null) mic.release();
            } catch (RuntimeException ignored) {
            }
            try {
                if (codec != null) {
                    codec.stop();
                    codec.release();
                }
            } catch (RuntimeException ignored) {
            }
        }
    }

    private void drain(MediaCodec codec, MediaCodec.BufferInfo info) {
        while (!stopped) {
            int out = codec.dequeueOutputBuffer(info, 0);
            if (out == MediaCodec.INFO_OUTPUT_FORMAT_CHANGED) {
                ByteBuffer csd = codec.getOutputFormat().getByteBuffer("csd-0");
                if (csd != null) sink.soundConfig(bytes(csd, csd.position(), csd.remaining()));
                continue;
            }
            if (out < 0) return;
            ByteBuffer buffer = codec.getOutputBuffer(out);
            byte[] data = buffer == null || info.size == 0 ? null : bytes(buffer, info.offset, info.size);
            codec.releaseOutputBuffer(out, false);
            if (data == null) continue;
            if ((info.flags & MediaCodec.BUFFER_FLAG_CODEC_CONFIG) != 0) sink.soundConfig(data);
            else sink.sound(data, info.presentationTimeUs);
        }
    }

    private static byte[] bytes(ByteBuffer buffer, int offset, int size) {
        byte[] out = new byte[size];
        ByteBuffer view = buffer.duplicate();
        view.position(offset);
        view.get(out, 0, size);
        return out;
    }
}
