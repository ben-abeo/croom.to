package to.croom.doorsign;

import android.content.Context;
import android.graphics.ImageFormat;
import android.media.Image;
import android.media.ImageReader;
import android.os.Handler;
import android.os.HandlerThread;
import android.os.Process;
import android.os.SystemClock;
import android.util.Log;
import android.view.Surface;

/**
 * Watches the camera's picture for people while the room's Door sign camera people detection is on (spec 2026-11-22). The camera
 * draws a second, small picture for it beside the one it sends. Five times a second one is looked at for movement (MotionGate), and
 * while something moves or someone is there, for people (PeopleModel); Presence makes of that the one thing Protect is told, whether
 * someone is there. All on a thread of its own, at a low priority, so the sign's page never waits for it.
 *
 * Whatever fails here costs only the telling apart, never the picture: without the model any movement counts as someone, and without
 * pictures someone is always there. The sink is told why, and so are the door sign's card and the panel's setup screen.
 */
final class PeopleWatch implements CameraStream.Second {

    interface Sink {
        /** Someone came into the picture, or the last one left it. */
        void people(boolean there);

        /** Why people are not being told from other movement, and what Protect is told instead. */
        void peopleTrouble(String why);
    }

    static final long PICTURE_MS = 200;
    static final String ANY_MOVEMENT = ". Protect is told of any movement instead";
    static final String ALWAYS = ". Protect is told there is movement all the time, so it keeps recording";
    static final String NOT_ASKED = "the camera failed " + SecondPicture.TRIES + " times while it drew a second picture, so it is not asked for one";

    private final Context context;
    private final Sink sink;
    private final boolean ask;              // whether the camera may be asked for the second picture at all (SecondPicture)
    private final HandlerThread thread = new HandlerThread("door-sign-people", Process.THREAD_PRIORITY_BACKGROUND);
    private final Frame frame = new Frame();
    private final MotionGate gate = new MotionGate();
    private final Presence presence = new Presence();
    private final int[] cells = new int[Frame.GRID_W * Frame.GRID_H];
    private final byte[] rgb = new byte[Frame.SIDE * Frame.SIDE * 3];
    private Handler handler;
    private PeopleModel model;
    private volatile ImageReader reader;
    private volatile long lastPicture;
    private volatile boolean closed;
    private volatile boolean drawing;
    private long analysed;
    private boolean told;
    private float sure;                     // the last look: how sure the model was of a person, and how long the look took
    private long lookMs;

    PeopleWatch(Context context, Sink sink, boolean ask) {
        this.context = context.getApplicationContext();
        this.sink = sink;
        this.ask = ask;
    }

    /** Whether the camera is drawing the second picture: it was asked for one and has not refused it. */
    boolean drawing() {
        return drawing;
    }

    /** Before the camera starts: the thread, and on it the model, so it is there (or known not to be) before the first picture. */
    void start() {
        thread.start();
        handler = new Handler(thread.getLooper());
        lastPicture = SystemClock.elapsedRealtime();
        handler.post(() -> {
            try {
                model = new PeopleModel(context);
            } catch (Throwable e) {     // an Error too (TensorFlow Lite's library not loading is one): nothing here may end the sign's process
                noModel("the people model did not load: " + words(e));
            }
        });
    }

    /** From the camera's keeper, now and then: pictures that stopped coming are said, rather than nobody being seen for ever. */
    void check(long stallMs) {
        if (handler == null) return;
        handler.post(() -> {
            if (presence.mode() != Presence.Mode.BLIND && SystemClock.elapsedRealtime() - lastPicture > stallMs) {
                blind("no picture to look at for " + stallMs / 1000 + " seconds");
            }
        });
    }

    // --- CameraStream.Second, on the camera's thread

    @Override public Surface surface(int[][] sizes) {
        if (!ask) {
            handler.post(() -> blind(NOT_ASKED));
            return null;
        }
        int[] size = Frame.pick(sizes);
        if (size == null) {
            refused("it has no size to look at");
            return null;
        }
        try {
            ImageReader made = ImageReader.newInstance(size[0], size[1], ImageFormat.YUV_420_888, 2);
            made.setOnImageAvailableListener(this::picture, handler);
            reader = made;
            lastPicture = SystemClock.elapsedRealtime();
            drawing = true;
            return made.getSurface();
        } catch (RuntimeException e) {
            refused(words(e));
            return null;
        }
    }

    @Override public void refused(String why) {
        drawing = false;
        handler.post(() -> blind("the camera gave no second picture: " + why));
    }

    @Override public void closed() {
        if (closed || handler == null) return;
        closed = true;
        handler.post(() -> {
            ImageReader made = reader;
            if (made != null) made.close();
            if (model != null) model.close();
            thread.quitSafely();
        });
    }

    // --- on its own thread

    private void picture(ImageReader from) {
        if (closed) return;
        Image image = null;
        try {
            image = from.acquireLatestImage();
            if (image == null) return;
            long now = SystemClock.elapsedRealtime();
            lastPicture = now;
            if (presence.mode() == Presence.Mode.BLIND || now - analysed < PICTURE_MS) return;
            analysed = now;
            Image.Plane[] planes = image.getPlanes();
            frame.luma(image.getWidth(), image.getHeight(), planes[0].getBuffer(), planes[0].getRowStride());
            frame.grid(cells);
            boolean people = presence.look(gate.moved(cells), now);
            if (people) frame.colour(planes[1].getBuffer(), planes[2].getBuffer(), planes[1].getRowStride(), planes[1].getPixelStride());
            image.close();                  // back to the camera before the model takes its time
            image = null;
            if (people) look(now);
            tell(presence.there(now));
        } catch (Throwable e) {             // nothing here may end the sign's process
            blind("the camera's second picture could not be read: " + words(e));
        } finally {
            if (image != null) image.close();
        }
    }

    private void look(long now) {
        try {
            frame.square(gate.lookRight(), rgb);
            sure = model.look(rgb);
            lookMs = SystemClock.elapsedRealtime() - now;
            presence.looked(sure >= PeopleModel.SURE, now);
        } catch (Throwable e) {
            noModel("the people model failed: " + words(e));
        }
    }

    private void noModel(String why) {
        if (presence.mode() != Presence.Mode.PEOPLE) return;
        presence.mode(Presence.Mode.MOVEMENT);
        sink.peopleTrouble(why + ANY_MOVEMENT);
    }

    private void blind(String why) {
        if (presence.mode() == Presence.Mode.BLIND) return;
        presence.mode(Presence.Mode.BLIND);
        sink.peopleTrouble(why + ALWAYS);
        tell(presence.there(SystemClock.elapsedRealtime()));
    }

    private void tell(boolean there) {
        if (there == told || closed) return;
        told = there;
        if (there && presence.mode() == Presence.Mode.PEOPLE) {     // for whoever tunes it on a panel: adb logcat -s DoorSign
            Log.i("DoorSign", "People: the model is " + Math.round(sure * 100) + "% sure, and a look took " + lookMs + " ms");
        }
        sink.people(there);
    }

    private static String words(Throwable e) {
        return e.getMessage() == null ? e.getClass().getSimpleName() : e.getMessage();
    }
}
