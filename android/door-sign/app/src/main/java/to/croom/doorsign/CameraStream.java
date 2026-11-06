package to.croom.doorsign;

import android.annotation.SuppressLint;
import android.content.Context;
import android.hardware.camera2.CameraAccessException;
import android.hardware.camera2.CameraCaptureSession;
import android.hardware.camera2.CameraCharacteristics;
import android.hardware.camera2.CameraDevice;
import android.hardware.camera2.CameraManager;
import android.hardware.camera2.CaptureRequest;
import android.media.MediaCodec;
import android.media.MediaCodecInfo;
import android.media.MediaFormat;
import android.os.Bundle;
import android.os.Handler;
import android.os.HandlerThread;
import android.util.Range;
import android.view.Surface;
import java.nio.ByteBuffer;
import java.util.Collections;
import java.util.List;

/**
 * The panel's front camera encoded to H.264 in hardware (spec 2026-11-06): Camera2 draws into MediaCodec's input surface, 1280 × 720 at
 * 15 frames a second, 1.5 Mbit/s, a keyframe every 2 seconds. Everything runs on a thread of its own, never the sign's; a camera or
 * encoder error is reported once to the sink, which decides what to do (the service stops it and tries again later).
 */
final class CameraStream {

    interface Sink {
        /** The encoder's SPS and PPS (without start codes), before the first picture. */
        void config(byte[] sps, byte[] pps);

        /** One encoded picture, Annex B. */
        void frame(byte[] annexB, long presentationUs, boolean keyframe);

        void failed(String why);
    }

    private final Context context;
    private final Sink sink;
    private final HandlerThread thread = new HandlerThread("door-sign-camera");
    private Handler handler;
    private MediaCodec codec;
    private Surface surface;
    private CameraDevice camera;
    private CameraCaptureSession session;
    private volatile boolean stopped;

    CameraStream(Context context, Sink sink) {
        this.context = context.getApplicationContext();
        this.sink = sink;
    }

    void start() {
        thread.start();
        handler = new Handler(thread.getLooper());
        handler.post(this::open);
    }

    /** Ask the encoder for a keyframe now: a new viewer starts with it rather than waiting up to 2 seconds. */
    void requestKeyframe() {
        Handler h = handler;
        if (h == null) return;
        h.post(() -> {
            if (codec == null || stopped) return;
            Bundle params = new Bundle();
            params.putInt(MediaCodec.PARAMETER_KEY_REQUEST_SYNC_FRAME, 0);
            try {
                codec.setParameters(params);
            } catch (RuntimeException e) {
                // the next scheduled keyframe comes within 2 seconds anyway
            }
        });
    }

    void stop() {
        stopped = true;
        Handler h = handler;
        if (h == null) return;
        h.post(() -> {
            close();
            thread.quitSafely();
        });
    }

    @SuppressLint("MissingPermission")   // granted as device owner before the service starts (CameraControl.ensure)
    private void open() {
        try {
            codec = encoder();
            surface = codec.createInputSurface();
            codec.start();
            CameraManager manager = context.getSystemService(CameraManager.class);
            manager.openCamera(frontCamera(manager), new CameraDevice.StateCallback() {
                @Override public void onOpened(CameraDevice device) {
                    camera = device;
                    if (stopped) close();
                    else capture(manager);
                }

                @Override public void onDisconnected(CameraDevice device) {
                    fail("the camera was disconnected");
                }

                @Override public void onError(CameraDevice device, int error) {
                    fail("camera error " + error);
                }
            }, handler);
        } catch (Exception e) {
            fail(String.valueOf(e.getMessage() == null ? e : e.getMessage()));
        }
    }

    private MediaCodec encoder() throws Exception {
        MediaCodec encoder = newEncoder();
        try {
            encoder.configure(format(true), null, null, MediaCodec.CONFIGURE_FLAG_ENCODE);
        } catch (RuntimeException e) {
            // The Main profile refused: a fresh encoder with its own choice of profile (reset() would also drop the callbacks below)
            encoder.release();
            encoder = newEncoder();
            encoder.configure(format(false), null, null, MediaCodec.CONFIGURE_FLAG_ENCODE);
        }
        return encoder;
    }

    private MediaCodec newEncoder() throws Exception {
        MediaCodec encoder = MediaCodec.createEncoderByType(MediaFormat.MIMETYPE_VIDEO_AVC);   // Android lists the hardware encoder first
        encoder.setCallback(new MediaCodec.Callback() {
            @Override public void onInputBufferAvailable(MediaCodec c, int index) {
                // input comes through the surface
            }

            @Override public void onOutputBufferAvailable(MediaCodec c, int index, MediaCodec.BufferInfo info) {
                output(c, index, info);
            }

            @Override public void onError(MediaCodec c, MediaCodec.CodecException e) {
                fail("the encoder failed: " + e.getDiagnosticInfo());
            }

            @Override public void onOutputFormatChanged(MediaCodec c, MediaFormat format) {
                try {
                    ByteBuffer sps = format.getByteBuffer("csd-0");
                    ByteBuffer pps = format.getByteBuffer("csd-1");
                    if (sps != null && pps != null) {
                        parameterSets(bytes(sps, sps.position(), sps.remaining()), bytes(pps, pps.position(), pps.remaining()));
                    }
                } catch (RuntimeException e) {          // on the camera's thread, in the sign's process: never let it end that
                    fail("the encoder's format could not be read: " + e.getMessage());
                }
            }
        }, handler);
        return encoder;
    }

    private static MediaFormat format(boolean mainProfile) {
        MediaFormat format = MediaFormat.createVideoFormat(MediaFormat.MIMETYPE_VIDEO_AVC, Onvif.WIDTH, Onvif.HEIGHT);
        format.setInteger(MediaFormat.KEY_COLOR_FORMAT, MediaCodecInfo.CodecCapabilities.COLOR_FormatSurface);
        format.setInteger(MediaFormat.KEY_BIT_RATE, Onvif.BITRATE_KBPS * 1000);
        format.setInteger(MediaFormat.KEY_FRAME_RATE, Onvif.FPS);
        format.setInteger(MediaFormat.KEY_I_FRAME_INTERVAL, Onvif.GOP / Onvif.FPS);
        if (mainProfile) {
            format.setInteger(MediaFormat.KEY_PROFILE, MediaCodecInfo.CodecProfileLevel.AVCProfileMain);
            format.setInteger(MediaFormat.KEY_LEVEL, MediaCodecInfo.CodecProfileLevel.AVCLevel31);
        }
        return format;
    }

    private static String frontCamera(CameraManager manager) throws CameraAccessException {
        String[] ids = manager.getCameraIdList();
        if (ids.length == 0) throw new IllegalStateException("the panel has no camera");
        for (String id : ids) {
            Integer facing = manager.getCameraCharacteristics(id).get(CameraCharacteristics.LENS_FACING);
            if (facing != null && facing == CameraCharacteristics.LENS_FACING_FRONT) return id;
        }
        return ids[0];
    }

    @SuppressWarnings("deprecation")   // createCaptureSession(List, …): the form Android 8, the app's oldest, has
    private void capture(CameraManager manager) {
        try {
            Range<Integer> fps = fpsRange(manager.getCameraCharacteristics(camera.getId())
                    .get(CameraCharacteristics.CONTROL_AE_AVAILABLE_TARGET_FPS_RANGES));
            List<Surface> targets = Collections.singletonList(surface);
            camera.createCaptureSession(targets, new CameraCaptureSession.StateCallback() {
                @Override public void onConfigured(CameraCaptureSession s) {
                    session = s;
                    try {
                        CaptureRequest.Builder request = camera.createCaptureRequest(CameraDevice.TEMPLATE_RECORD);
                        request.addTarget(surface);
                        if (fps != null) request.set(CaptureRequest.CONTROL_AE_TARGET_FPS_RANGE, fps);
                        s.setRepeatingRequest(request.build(), null, handler);
                    } catch (Exception e) {
                        fail("the camera would not start: " + e.getMessage());
                    }
                }

                @Override public void onConfigureFailed(CameraCaptureSession s) {
                    fail("the camera would not take the encoder's picture size");
                }
            }, handler);
        } catch (Exception e) {
            fail("the camera would not start: " + e.getMessage());
        }
    }

    /** [15, 15] when the camera has it, else the narrowest range with 15 in it, else none (the camera's default). */
    static Range<Integer> fpsRange(Range<Integer>[] ranges) {
        if (ranges == null) return null;
        Range<Integer> best = null;
        for (Range<Integer> r : ranges) {
            if (r.getLower() > Onvif.FPS || r.getUpper() < Onvif.FPS) continue;
            if (best == null || r.getUpper() - r.getLower() < best.getUpper() - best.getLower()) best = r;
        }
        return best;
    }

    private void output(MediaCodec c, int index, MediaCodec.BufferInfo info) {
        try {
            ByteBuffer buffer = c.getOutputBuffer(index);
            byte[] data = buffer == null || info.size == 0 ? null : bytes(buffer, info.offset, info.size);
            c.releaseOutputBuffer(index, false);
            if (data == null || stopped) return;
            if ((info.flags & MediaCodec.BUFFER_FLAG_CODEC_CONFIG) != 0) {
                byte[] sps = null;
                byte[] pps = null;
                for (byte[] nal : H264Rtp.nals(data)) {
                    if ((nal[0] & 0x1f) == 7) sps = nal;
                    if ((nal[0] & 0x1f) == 8) pps = nal;
                }
                if (sps != null && pps != null) parameterSets(sps, pps);
                return;
            }
            sink.frame(data, info.presentationTimeUs, (info.flags & MediaCodec.BUFFER_FLAG_KEY_FRAME) != 0);
        } catch (RuntimeException e) {
            fail("the encoder failed: " + e.getMessage());
        }
    }

    private void parameterSets(byte[] sps, byte[] pps) {
        // csd buffers carry start codes; the codec-config buffer's NALs have them stripped already
        List<byte[]> s = H264Rtp.nals(sps);
        List<byte[]> p = H264Rtp.nals(pps);
        sink.config(s.isEmpty() ? sps : s.get(0), p.isEmpty() ? pps : p.get(0));
    }

    private static byte[] bytes(ByteBuffer buffer, int offset, int size) {
        byte[] out = new byte[size];
        ByteBuffer view = buffer.duplicate();
        view.position(offset);
        view.get(out, 0, size);
        return out;
    }

    private void fail(String why) {
        if (stopped) return;
        stopped = true;
        close();
        sink.failed(why);
    }

    private void close() {
        try {
            if (session != null) session.close();
        } catch (RuntimeException ignored) {
        }
        try {
            if (camera != null) camera.close();
        } catch (RuntimeException ignored) {
        }
        try {
            if (codec != null) {
                codec.stop();
                codec.release();
            }
        } catch (RuntimeException ignored) {
        }
        if (surface != null) surface.release();
        session = null;
        camera = null;
        codec = null;
        surface = null;
    }
}
