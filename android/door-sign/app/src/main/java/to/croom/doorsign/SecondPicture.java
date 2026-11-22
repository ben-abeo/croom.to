package to.croom.doorsign;

/**
 * Whether the camera may be asked for a second picture, for people detection (spec 2026-11-22). A camera that will not draw two
 * pictures says so, and the stream starts without the second (CameraStream). But a camera may take both and then fail, and the
 * service starts a failed camera again every 10 seconds: asked for two pictures every time, it would never record again. So a camera
 * that fails TRIES times running while it draws a second picture is not asked for one any more, until the room sends another login or
 * the panel restarts. A camera that ran for PROOF_MS with the second picture has shown it can, and its failure is not counted.
 */
final class SecondPicture {

    static final int TRIES = 2;
    static final long PROOF_MS = 10 * 60_000;

    private int failures;

    boolean allowed() {
        return failures < TRIES;
    }

    /** The camera failed after running `ranMs` while it drew a second picture. */
    void failed(long ranMs) {
        failures = ranMs >= PROOF_MS ? 0 : failures + 1;
    }

    /** A new login: ask the camera afresh. */
    void again() {
        failures = 0;
    }
}
