package to.croom.doorsign;

import static org.junit.Assert.assertArrayEquals;
import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertNull;
import static org.junit.Assert.assertTrue;

import java.nio.ByteBuffer;
import org.junit.Test;

public class FrameTest {

    /** A picture as the camera's buffer holds it: rows longer than the picture is wide, colour at half size, `pixelStride` bytes apart. */
    static final class Picture {
        final int width;
        final int height;
        final int rowStride;
        final int pixelStride;
        final byte[] luma;
        final byte[] u;
        final byte[] v;

        Picture(int width, int height, int padding, int pixelStride) {
            this.width = width;
            this.height = height;
            this.rowStride = width + padding;
            this.pixelStride = pixelStride;
            luma = new byte[rowStride * (height - 1) + width];          // the last row has no padding, as Android's buffers
            u = new byte[rowStride * (height / 2 - 1) + (width / 2 - 1) * pixelStride + 1];
            v = new byte[u.length];
            java.util.Arrays.fill(luma, (byte) 0x7f);                   // anything read from the padding would show
            java.util.Arrays.fill(u, (byte) 128);
            java.util.Arrays.fill(v, (byte) 128);
        }

        void set(int x, int y, int l, int cb, int cr) {
            luma[y * rowStride + x] = (byte) l;
            u[(y / 2) * rowStride + (x / 2) * pixelStride] = (byte) cb;
            v[(y / 2) * rowStride + (x / 2) * pixelStride] = (byte) cr;
        }

        void fill(int x0, int y0, int x1, int y1, int l, int cb, int cr) {
            for (int y = y0; y < y1; y++) for (int x = x0; x < x1; x++) set(x, y, l, cb, cr);
        }

        Frame frame() {
            Frame frame = new Frame();
            frame.luma(width, height, ByteBuffer.wrap(luma), rowStride);
            frame.colour(ByteBuffer.wrap(u), ByteBuffer.wrap(v), rowStride, pixelStride);
            return frame;
        }
    }

    static int[] rgb(byte[] square, int x, int y) {
        int at = (y * Frame.SIDE + x) * 3;
        return new int[] {square[at] & 0xff, square[at + 1] & 0xff, square[at + 2] & 0xff};
    }

    @Test public void theSmallestSizeThatCoversTwoSquaresIsAskedFor() {
        int[][] sizes = {{1920, 1080}, {1280, 720}, {640, 480}, {320, 240}, {2592, 1944}, {640, 360}, {800, 600}};
        assertArrayEquals(new int[] {640, 360}, Frame.pick(sizes));
        assertArrayEquals(new int[] {640, 480}, Frame.pick(new int[][] {{1920, 1080}, {640, 480}, {352, 288}}));
        assertArrayEquals(new int[] {1280, 720}, Frame.pick(new int[][] {{1280, 720}, {480, 640}, {320, 240}}));     // 480 × 640 is too narrow
    }

    @Test public void aCameraWithOnlySmallSizesGivesItsLargest() {
        assertArrayEquals(new int[] {352, 288}, Frame.pick(new int[][] {{176, 144}, {352, 288}, {320, 240}}));
        assertNull(Frame.pick(new int[0][]));
        assertNull(Frame.pick(null));
    }

    @Test public void onlyTheMiddleSixteenByNineIsLookedAt() {
        assertArrayEquals(new int[] {0, 60, 640, 360}, new Picture(640, 480, 0, 1).frame().crop());
        assertArrayEquals(new int[] {0, 0, 640, 360}, new Picture(640, 360, 0, 1).frame().crop());
        assertArrayEquals(new int[] {40, 0, 640, 360}, new Picture(720, 360, 0, 1).frame().crop());      // wider than 16:9: its middle
    }

    @Test public void theGridIsEachCellsBrightnessAndIgnoresWhatIsOutsideTheMiddle() {
        Picture picture = new Picture(640, 480, 32, 2);
        picture.fill(0, 0, 640, 480, 50, 128, 128);
        picture.fill(0, 0, 640, 60, 255, 128, 128);                  // above the middle 16:9: not looked at
        picture.fill(0, 420, 640, 480, 255, 128, 128);               // and below it
        picture.fill(320, 60 + 180, 340, 60 + 200, 200, 128, 128);   // one cell, 16 across and 9 down, bright
        int[] cells = new int[Frame.GRID_W * Frame.GRID_H];
        picture.frame().grid(cells);
        for (int i = 0; i < cells.length; i++) assertEquals("cell " + i, i == 9 * Frame.GRID_W + 16 ? 200 : 50, cells[i]);
    }

    @Test public void theSquaresAreTheLeftAndTheRightOfTheMiddle() {
        Picture picture = new Picture(640, 480, 16, 2);
        picture.fill(0, 0, 640, 480, 40, 128, 128);                  // dark grey
        picture.fill(0, 60, 280, 420, 200, 128, 128);                // the left 280 columns light: only the left square has them
        picture.fill(360, 60, 640, 420, 120, 128, 128);              // the right 280 mid grey: only the right square has them
        Frame frame = picture.frame();
        byte[] left = new byte[Frame.SIDE * Frame.SIDE * 3];
        byte[] right = new byte[left.length];
        frame.square(false, left);
        frame.square(true, right);
        // 360 picture columns onto 320: the left square is columns 0 to 359, the right one 280 to 639
        assertArrayEquals(new int[] {200, 200, 200}, rgb(left, 0, 0));
        assertArrayEquals(new int[] {200, 200, 200}, rgb(left, 240, 319));
        assertArrayEquals(new int[] {40, 40, 40}, rgb(left, 260, 160));          // the overlap, columns 280 to 359
        assertArrayEquals(new int[] {40, 40, 40}, rgb(right, 60, 160));
        assertArrayEquals(new int[] {120, 120, 120}, rgb(right, 80, 0));
        assertArrayEquals(new int[] {120, 120, 120}, rgb(right, 319, 319));
    }

    @Test public void coloursComeOutAsRedGreenAndBlue() {
        for (int pixelStride : new int[] {1, 2}) {                   // the colour planes apart, or woven into one another
            Picture picture = new Picture(640, 360, 0, pixelStride);
            picture.fill(0, 0, 640, 360, 81, 90, 240);               // red, as cameras encode it
            picture.fill(0, 180, 640, 360, 41, 240, 110);            // blue below
            byte[] square = new byte[Frame.SIDE * Frame.SIDE * 3];
            picture.frame().square(false, square);
            int[] red = rgb(square, 100, 50);
            int[] blue = rgb(square, 100, 300);
            assertTrue(java.util.Arrays.toString(red), red[0] > 200 && red[1] < 60 && red[2] < 60);
            assertTrue(java.util.Arrays.toString(blue), blue[2] > 200 && blue[0] < 60 && blue[1] < 60);
        }
    }

    @Test public void theNextPictureMayBeAnotherSize() {
        Frame frame = new Picture(640, 480, 0, 2).frame();
        Picture small = new Picture(320, 240, 8, 1);
        small.fill(0, 0, 320, 240, 90, 128, 128);
        frame.luma(small.width, small.height, ByteBuffer.wrap(small.luma), small.rowStride);
        frame.colour(ByteBuffer.wrap(small.u), ByteBuffer.wrap(small.v), small.rowStride, small.pixelStride);
        int[] cells = new int[Frame.GRID_W * Frame.GRID_H];
        frame.grid(cells);
        assertEquals(90, cells[0]);
        byte[] square = new byte[Frame.SIDE * Frame.SIDE * 3];
        frame.square(true, square);                                  // a 180-row middle, made larger for the model
        assertArrayEquals(new int[] {90, 90, 90}, rgb(square, 319, 319));
    }
}
