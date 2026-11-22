package to.croom.doorsign;

import java.nio.ByteBuffer;

/**
 * One small picture from the camera to look at for people (spec 2026-11-22), as Android hands it over: a brightness plane and two
 * colour planes at half its size (YUV 4:2:0), each with its own row length. Copied out of the camera's buffer so that goes straight
 * back. Only the middle 16:9 of it is looked at, which is what the camera's stream shows Protect. From it come the brightness of a
 * grid of cells (for MotionGate) and the model's squares: the left and the right of the picture, each as tall as it, which overlap in
 * the middle. Two squares rather than the whole picture made to fit one, because a person twice as far away is still found that way.
 */
final class Frame {

    static final int GRID_W = 32;
    static final int GRID_H = 18;
    static final int SIDE = 320;                    // the model's square
    static final int WANT_W = 640;                  // enough of a picture for two squares of SIDE: more only costs
    static final int WANT_H = 360;
    private static final int SAMPLES = 5;           // a cell's brightness is the mean of SAMPLES × SAMPLES points in it

    private int width;
    private int height;
    private int rowStride;
    private int colourRowStride;
    private int colourPixelStride;
    private byte[] luma = new byte[0];
    private byte[] u = new byte[0];
    private byte[] v = new byte[0];

    /**
     * Which of the sizes the camera offers ({width, height} each) to ask for: the fewest pixels whose middle 16:9 is at least
     * WANT_W × WANT_H, else the most pixels there are. Null when it offers none.
     */
    static int[] pick(int[][] sizes) {
        int[] enough = null;
        int[] most = null;
        if (sizes != null) {
            for (int[] size : sizes) {
                if (size == null || size.length != 2 || size[0] <= 0 || size[1] <= 0) continue;
                long pixels = (long) size[0] * size[1];
                if (most == null || pixels > (long) most[0] * most[1]) most = size;
                boolean covers = Math.min(size[0], size[1] * 16 / 9) >= WANT_W && Math.min(size[1], size[0] * 9 / 16) >= WANT_H;
                if (covers && (enough == null || pixels < (long) enough[0] * enough[1])) enough = size;
            }
        }
        return enough != null ? enough : most;
    }

    void luma(int width, int height, ByteBuffer plane, int rowStride) {
        this.width = width;
        this.height = height;
        this.rowStride = rowStride;
        luma = copy(plane, luma);
    }

    void colour(ByteBuffer uPlane, ByteBuffer vPlane, int rowStride, int pixelStride) {
        colourRowStride = rowStride;
        colourPixelStride = pixelStride;
        u = copy(uPlane, u);
        v = copy(vPlane, v);
    }

    /** The middle 16:9 of the picture: {left, top, width, height}. */
    int[] crop() {
        int w = Math.min(width, height * 16 / 9);
        int h = Math.min(height, width * 9 / 16);
        return new int[] {(width - w) / 2, (height - h) / 2, w, h};
    }

    /** The brightness (0 to 255) of each cell of a GRID_W × GRID_H grid over the middle 16:9, row by row. */
    void grid(int[] cells) {
        int[] crop = crop();
        for (int gy = 0; gy < GRID_H; gy++) {
            for (int gx = 0; gx < GRID_W; gx++) {
                int sum = 0;
                for (int j = 0; j < SAMPLES; j++) {
                    int y = crop[1] + ((gy * SAMPLES + j) * 2 + 1) * crop[3] / (GRID_H * SAMPLES * 2);
                    for (int i = 0; i < SAMPLES; i++) {
                        int x = crop[0] + ((gx * SAMPLES + i) * 2 + 1) * crop[2] / (GRID_W * SAMPLES * 2);
                        sum += luma[y * rowStride + x] & 0xff;
                    }
                }
                cells[gy * GRID_W + gx] = sum / (SAMPLES * SAMPLES);
            }
        }
    }

    /** The left or the right square of the middle 16:9 as the model takes it: SIDE × SIDE, red, green and blue a byte each. */
    void square(boolean right, byte[] rgb) {
        int[] crop = crop();
        int side = Math.min(crop[2], crop[3]);
        int left = right ? crop[0] + crop[2] - side : crop[0];
        int at = 0;
        for (int oy = 0; oy < SIDE; oy++) {
            int y = crop[1] + (oy * 2 + 1) * side / (SIDE * 2);
            int row = y * rowStride;
            int colourRow = (y / 2) * colourRowStride;
            for (int ox = 0; ox < SIDE; ox++) {
                int x = left + (ox * 2 + 1) * side / (SIDE * 2);
                int l = luma[row + x] & 0xff;
                int colourAt = colourRow + (x / 2) * colourPixelStride;
                int cb = (u[colourAt] & 0xff) - 128;
                int cr = (v[colourAt] & 0xff) - 128;
                rgb[at++] = clamp(l + (359 * cr >> 8));
                rgb[at++] = clamp(l - (88 * cb + 183 * cr >> 8));
                rgb[at++] = clamp(l + (454 * cb >> 8));
            }
        }
    }

    private static byte clamp(int value) {
        return (byte) (value < 0 ? 0 : Math.min(value, 255));
    }

    private static byte[] copy(ByteBuffer plane, byte[] into) {
        ByteBuffer view = plane.duplicate();
        view.rewind();
        byte[] out = into.length == view.remaining() ? into : new byte[view.remaining()];
        view.get(out);
        return out;
    }
}
