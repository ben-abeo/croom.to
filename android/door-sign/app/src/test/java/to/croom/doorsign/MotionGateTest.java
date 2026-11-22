package to.croom.doorsign;

import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertTrue;

import java.util.Arrays;
import java.util.Random;
import org.junit.Test;

public class MotionGateTest {

    static int[] room(int brightness) {
        int[] cells = new int[Frame.GRID_W * Frame.GRID_H];
        Arrays.fill(cells, brightness);
        return cells;
    }

    /** The room with someone in it: a block of cells `wide` by `tall` from (x, y), darker than the room. */
    static int[] someone(int brightness, int x, int y, int wide, int tall) {
        int[] cells = room(brightness);
        for (int j = y; j < y + tall; j++) for (int i = x; i < x + wide; i++) cells[j * Frame.GRID_W + i] = brightness - 60;
        return cells;
    }

    @Test public void anEmptyRoomDoesNotMove() {
        MotionGate gate = new MotionGate();
        for (int i = 0; i < 20; i++) assertFalse(gate.moved(room(120)));
    }

    @Test public void theFirstPictureIsNeverMovement() {
        assertFalse(new MotionGate().moved(someone(120, 4, 4, 3, 8)));
    }

    @Test public void someoneWalkingInMoves() {
        MotionGate gate = new MotionGate();
        gate.moved(room(120));
        assertTrue(gate.moved(someone(120, 4, 6, 2, 8)));
        assertTrue(gate.moved(someone(120, 8, 6, 2, 8)));
    }

    @Test public void someoneFarAwayIsAFewCellsAndStillMoves() {
        MotionGate gate = new MotionGate();
        gate.moved(room(120));
        assertTrue(gate.moved(someone(120, 20, 10, 1, 2)));
    }

    @Test public void oneCellAloneIsNoise() {
        MotionGate gate = new MotionGate();
        gate.moved(room(120));
        assertFalse(gate.moved(someone(120, 20, 10, 1, 1)));
    }

    @Test public void theCamerasNoiseIsNotMovement() {
        MotionGate gate = new MotionGate();
        Random random = new Random(7);
        for (int n = 0; n < 50; n++) {
            int[] cells = room(120);
            for (int i = 0; i < cells.length; i++) cells[i] += random.nextInt(7) - 3;
            assertFalse("picture " + n, gate.moved(cells));
        }
    }

    @Test public void theLightChangingIsNotMovementButSomeoneInItIs() {
        MotionGate gate = new MotionGate();
        gate.moved(room(120));
        assertFalse(gate.moved(room(170)));                    // the lights went up: every cell alike
        assertFalse(gate.moved(room(90)));
        assertTrue(gate.moved(someone(140, 4, 6, 2, 8)));      // lighter again, and someone came in with it
    }

    @Test public void someoneStandingStillStopsBeingMovement() {
        MotionGate gate = new MotionGate();
        gate.moved(room(120));
        assertTrue(gate.moved(someone(120, 4, 6, 2, 8)));
        boolean moved = true;
        for (int i = 0; i < 20; i++) moved = gate.moved(someone(120, 4, 6, 2, 8));
        assertFalse(moved);
    }

    @Test public void theSquareThatMovedIsLookedAt() {
        MotionGate gate = new MotionGate();
        gate.moved(room(120));
        gate.moved(someone(120, 2, 6, 2, 8));                  // at the left only
        assertFalse(gate.lookRight());
        assertFalse(gate.lookRight());
        MotionGate other = new MotionGate();
        other.moved(room(120));
        other.moved(someone(120, 28, 6, 2, 8));                // at the right only
        assertTrue(other.lookRight());
        assertTrue(other.lookRight());
    }

    @Test public void whenBothSquaresMovedTheyTakeTurnsHoweverMuchEachDid() {
        MotionGate gate = new MotionGate();
        gate.moved(room(120));
        int[] cells = someone(120, 0, 0, 10, 18);              // a door swinging across the left
        cells[10 * Frame.GRID_W + 30] -= 60;                   // and someone far away at the right
        cells[11 * Frame.GRID_W + 30] -= 60;
        gate.moved(cells);
        boolean first = gate.lookRight();
        assertTrue(first != gate.lookRight());
        assertTrue(first == gate.lookRight());
    }

    @Test public void withNothingToChooseByTheSquaresTakeTurns() {
        MotionGate gate = new MotionGate();
        gate.moved(room(120));
        gate.moved(room(120));                                 // nothing moved: someone standing still is looked for in both
        boolean first = gate.lookRight();
        assertTrue(first != gate.lookRight());
        assertTrue(first == gate.lookRight());
        gate.moved(someone(120, 15, 6, 2, 8));                 // in the middle, which both squares have
        assertTrue(first != gate.lookRight());
    }
}
