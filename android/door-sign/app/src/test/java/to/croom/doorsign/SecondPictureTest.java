package to.croom.doorsign;

import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertTrue;

import org.junit.Test;

public class SecondPictureTest {

    @Test public void theCameraIsAskedUntilItHasFailedTwiceRunningWithIt() {
        SecondPicture second = new SecondPicture();
        assertTrue(second.allowed());
        second.failed(16_000);                          // no picture for 15 seconds, soon after it started
        assertTrue(second.allowed());                   // once may be chance: asked again
        second.failed(16_000);
        assertFalse(second.allowed());                  // twice running: the stream goes on without, and records
        second.failed(16_000);
        assertFalse(second.allowed());
    }

    @Test public void aCameraThatRanLongWithItHasShownItCan() {
        SecondPicture second = new SecondPicture();
        second.failed(5_000);
        second.failed(SecondPicture.PROOF_MS);          // ten minutes of two pictures, then a failure: not the second picture's doing
        assertTrue(second.allowed());
        second.failed(5_000);
        assertTrue(second.allowed());
        second.failed(SecondPicture.PROOF_MS - 1);
        assertFalse(second.allowed());
    }

    @Test public void aNewLoginAsksAfresh() {
        SecondPicture second = new SecondPicture();
        second.failed(5_000);
        second.failed(5_000);
        second.again();
        assertTrue(second.allowed());
    }
}
