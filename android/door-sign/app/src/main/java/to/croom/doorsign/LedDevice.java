package to.croom.doorsign;

import android.util.Log;

/** The panel's light controller, /dev/ledjni, through libledbar (src/main/cpp/ledbar.c). */
final class LedDevice implements LightBar.Device {

    private static final String TAG = "DoorSign";
    private static final boolean LOADED = load();

    private int fd = -1;

    @Override public boolean open() {
        if (!LOADED) return false;
        fd = nativeOpen();
        if (fd < 0) Log.w(TAG, "No light bar: /dev/ledjni does not open on this tablet");
        return fd >= 0;
    }

    @Override public void set(int channel, int level) {
        if (nativeSet(fd, channel, level) < 0) Log.w(TAG, "The light bar refused channel " + channel);
    }

    @Override public void close() {
        if (fd >= 0) nativeClose(fd);
        fd = -1;
    }

    private static boolean load() {
        try {
            System.loadLibrary("ledbar");
            return true;
        } catch (UnsatisfiedLinkError e) {
            Log.w(TAG, "No light bar library for this tablet", e);
            return false;
        }
    }

    private static native int nativeOpen();

    private static native int nativeSet(int fd, int channel, int level);

    private static native void nativeClose(int fd);
}
