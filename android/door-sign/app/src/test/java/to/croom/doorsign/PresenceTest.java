package to.croom.doorsign;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertTrue;

import org.junit.Test;

public class PresenceTest {

    /** Pictures 200 ms apart from `from` to `to`, each with or without movement, each look finding `person`. Returns the looks made. */
    static int run(Presence presence, long from, long to, boolean movement, boolean person) {
        int looks = 0;
        for (long now = from; now <= to; now += 200) {
            if (presence.look(movement, now)) {
                looks++;
                presence.looked(person, now);
            }
            presence.there(now);
        }
        return looks;
    }

    @Test public void anEmptyStillHallIsNeverLookedAt() {
        Presence presence = new Presence();
        assertEquals(0, run(presence, 0, 60_000, false, false));
        assertFalse(presence.there(60_000));
    }

    @Test public void someoneWalkingInIsThereAtTheFirstSightOfThem() {
        Presence presence = new Presence();
        assertTrue(presence.look(true, 1_000));
        presence.looked(true, 1_000);
        assertTrue(presence.there(1_000));
    }

    @Test public void movementThatIsNotAPersonIsNobody() {
        Presence presence = new Presence();
        int looks = run(presence, 0, 5_000, true, false);
        assertTrue("looks: " + looks, looks >= 10);             // a door swinging is looked at, and is nobody
        assertFalse(presence.there(5_000));
    }

    @Test public void looksAreNoMoreOftenThanEveryLookMs() {
        Presence presence = new Presence();
        assertTrue(presence.look(true, 1_000));
        assertFalse(presence.look(true, 1_100));
        assertFalse(presence.look(true, 1_249));
        assertTrue(presence.look(true, 1_250));
    }

    @Test public void lookingGoesOnForAWhileAfterTheMovementStops() {
        Presence presence = new Presence();
        presence.look(true, 1_000);
        assertTrue(presence.look(false, 1_000 + Presence.TAIL_MS));
        assertFalse(presence.look(false, 1_001 + Presence.TAIL_MS + Presence.LOOK_MS));
    }

    @Test public void someoneWhoLeavesIsGoneAfterTheHold() {
        Presence presence = new Presence();
        assertTrue(presence.look(true, 3_000));
        presence.looked(true, 3_000);                                      // the last sight of them
        run(presence, 3_300, 3_000 + Presence.HOLD_MS, true, false);      // still movement (a door closing), nobody seen
        assertTrue(presence.there(3_000 + Presence.HOLD_MS));
        assertFalse(presence.there(3_001 + Presence.HOLD_MS));
    }

    @Test public void someoneStandingStillIsStillThereAndStillLookedFor() {
        Presence presence = new Presence();
        run(presence, 0, 2_000, true, true);
        int looks = run(presence, 2_200, 30_000, false, true);            // no movement at all, and the model still sees them
        assertTrue("looks: " + looks, looks >= 50);
        assertTrue(presence.there(30_000));
    }

    @Test public void aPictureOfAPersonIsNotThereOnceNothingHasMovedForLong() {
        Presence presence = new Presence();
        run(presence, 0, 1_000, true, true);                               // something moved, and the poster was "seen"
        run(presence, 1_200, 1_000 + Presence.STILL_MS, false, true);
        assertTrue(presence.there(1_000 + Presence.STILL_MS));
        assertFalse(presence.there(1_001 + Presence.STILL_MS));
        assertEquals(0, run(presence, 1_200 + Presence.STILL_MS, 200_000, false, true));     // and it is not looked at again until something moves
        assertFalse(presence.there(200_000));
    }

    @Test public void someoneWhoTurnsAwayAndBackIsOneVisit() {
        Presence presence = new Presence();
        boolean left = false;
        for (long now = 0; now <= 30_000; now += 200) {
            boolean seen = now % 8_000 < 2_000;                            // seen for 2 seconds in every 8
            if (presence.look(true, now)) presence.looked(seen, now);
            if (now > 0 && !presence.there(now)) left = true;
        }
        assertFalse(left);
    }

    @Test public void withoutTheModelAnyMovementIsSomeone() {
        Presence presence = new Presence();
        presence.mode(Presence.Mode.MOVEMENT);
        assertFalse(presence.there(0));
        assertFalse(presence.look(true, 1_000));                           // nothing to look with
        assertTrue(presence.there(1_000));
        assertTrue(presence.there(1_000 + Presence.HOLD_MS));
        assertFalse(presence.there(1_001 + Presence.HOLD_MS));
    }

    @Test public void withoutPicturesSomeoneIsAlwaysThere() {
        Presence presence = new Presence();
        presence.mode(Presence.Mode.BLIND);
        assertTrue(presence.there(0));
        assertFalse(presence.look(true, 1_000));
        assertTrue(presence.there(1_000_000));
    }

    @Test public void whatItCanTellOnlyEverGetsLess() {
        Presence presence = new Presence();
        presence.mode(Presence.Mode.BLIND);
        presence.mode(Presence.Mode.MOVEMENT);
        presence.mode(Presence.Mode.PEOPLE);
        assertEquals(Presence.Mode.BLIND, presence.mode());
    }
}
