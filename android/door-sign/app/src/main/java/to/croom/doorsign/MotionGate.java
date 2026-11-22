package to.croom.doorsign;

import java.util.Arrays;

/**
 * Whether anything moved in the camera's picture (spec 2026-11-22): each cell of Frame's grid against what that cell has looked like
 * lately. It costs next to nothing, so it runs on every picture looked at, and the people model, which costs a lot, runs only when it
 * says something moved. The room's light changing, or the camera settling its exposure, shifts every cell alike: that shift (the
 * middle one of all the cells') is taken off first, so it is not movement.
 */
final class MotionGate {

    static final int DELTA = 10;                // a cell this much brighter or darker than lately (of 255) has moved
    static final int MIN_CELLS = 2;             // this many moved cells is movement: one alone is the camera's noise
    static final int SETTLE = 4;                // how fast a cell's "lately" follows it: a quarter of the way each picture
    // The model's squares in cells: each as wide as the grid is tall, at the left and at the right, overlapping in the middle
    static final int LEFT_END = Frame.GRID_H;
    static final int RIGHT_START = Frame.GRID_W - Frame.GRID_H;

    private final float[] lately = new float[Frame.GRID_W * Frame.GRID_H];
    private final int[] change = new int[lately.length];
    private final int[] sorted = new int[lately.length];
    private boolean started;
    private int left;
    private int right;
    private boolean lookedRight;

    /** Takes the next picture's grid: whether something moved since the pictures before it. The first picture is never movement. */
    boolean moved(int[] cells) {
        left = 0;
        right = 0;
        if (!started) {
            for (int i = 0; i < lately.length; i++) lately[i] = cells[i];
            started = true;
            return false;
        }
        for (int i = 0; i < lately.length; i++) change[i] = cells[i] - Math.round(lately[i]);
        System.arraycopy(change, 0, sorted, 0, change.length);
        Arrays.sort(sorted);
        int shift = sorted[sorted.length / 2];
        int count = 0;
        for (int i = 0; i < lately.length; i++) {
            if (Math.abs(change[i] - shift) > DELTA) {
                count++;
                int column = i % Frame.GRID_W;
                if (column < LEFT_END) left++;
                if (column >= RIGHT_START) right++;
            }
            lately[i] += (cells[i] - lately[i]) / SETTLE;
        }
        return count >= MIN_CELLS;
    }

    /**
     * Which of the model's two squares to look at after the last picture: the one that moved when only one did, else the other one
     * than last time. So both are looked at in turn when both moved or neither did (someone standing still), and a door swinging in
     * one square never keeps the looks from someone far away in the other.
     */
    boolean lookRight() {
        if (left > 0 && right == 0) lookedRight = false;
        else if (right > 0 && left == 0) lookedRight = true;
        else lookedRight = !lookedRight;
        return lookedRight;
    }
}
