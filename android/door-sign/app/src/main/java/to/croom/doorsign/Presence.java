package to.croom.doorsign;

/**
 * Whether someone is in the camera's picture, which is what Protect is told as motion (spec 2026-11-22). Looking for people costs,
 * so it is done only while something moved in the last TAIL_MS or someone is there, and then every LOOK_MS. Someone seen is there for
 * HOLD_MS after the last sight of them, so a person who turns away or passes behind a pillar is one visit, not three. If nothing at
 * all has moved for STILL_MS they are not there, however sure the model is: a poster of a person is not one.
 *
 * A security camera must not go quiet because its detection broke, so when it cannot tell people it tells more, never less: without
 * the model (MOVEMENT) any movement counts as someone, and without pictures to look at (BLIND) someone is always there, so Protect
 * keeps recording.
 */
final class Presence {

    enum Mode { PEOPLE, MOVEMENT, BLIND }

    static final long LOOK_MS = 250;
    static final long TAIL_MS = 2_000;
    static final long HOLD_MS = 10_000;
    static final long STILL_MS = 60_000;
    private static final long NEVER = Long.MIN_VALUE / 2;

    private Mode mode = Mode.PEOPLE;
    private long moved = NEVER;
    private long seen = NEVER;
    private long looked = NEVER;
    private boolean there;

    Mode mode() {
        return mode;
    }

    /** What it can still tell. It only ever gets less: BLIND stays BLIND until the camera starts again. */
    void mode(Mode to) {
        if (to.ordinal() > mode.ordinal()) mode = to;
    }

    /** A picture was looked at for movement at `now` (milliseconds on any steady clock). Whether to look for people in it too. */
    boolean look(boolean movement, long now) {
        if (movement) moved = now;
        if (mode != Mode.PEOPLE) return false;
        if (!there && now - moved > TAIL_MS) return false;
        if (now - looked < LOOK_MS) return false;
        looked = now;
        return true;
    }

    /** What looking for people found. */
    void looked(boolean person, long now) {
        if (person) seen = now;
    }

    /** Whether someone is there at `now`. */
    boolean there(long now) {
        switch (mode) {
            case BLIND:
                there = true;
                break;
            case MOVEMENT:
                there = now - moved <= HOLD_MS;
                break;
            default:
                there = now - seen <= HOLD_MS && now - moved <= STILL_MS;
        }
        return there;
    }
}
