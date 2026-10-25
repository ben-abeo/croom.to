package to.croom.doorsign;

import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertNotEquals;
import static org.junit.Assert.assertTrue;

import org.junit.Test;

public class PinTest {

    @Test public void aPinIsFourToEightDigits() {
        assertTrue(Pin.valid("1234"));
        assertTrue(Pin.valid("12345678"));
        assertFalse(Pin.valid("123"));
        assertFalse(Pin.valid("123456789"));
        assertFalse(Pin.valid("12a4"));
        assertFalse(Pin.valid(""));
        assertFalse(Pin.valid(null));
    }

    @Test public void theRightPinMatchesAndNoOtherDoes() {
        Pin.Stored stored = Pin.store("2468");
        assertTrue(Pin.matches("2468", stored));
        assertFalse(Pin.matches("2469", stored));
        assertFalse(Pin.matches("", stored));
        assertFalse(Pin.matches(null, stored));
    }

    @Test public void thePinItselfIsNeverWhatIsStored() {
        Pin.Stored stored = Pin.store("2468");
        assertFalse(stored.hash.contains("2468"));
        assertNotEquals(stored.salt, Pin.store("2468").salt);   // the same PIN stores differently each time
    }

    @Test public void noStoredPinMatchesNothing() {
        assertFalse(Pin.matches("2468", null));
    }
}
