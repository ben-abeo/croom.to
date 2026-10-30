package to.croom.doorsign;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertNotEquals;
import static org.junit.Assert.assertTrue;

import java.security.SecureRandom;
import org.junit.Test;

public class IdentityTest {

    @Test public void madeOnceAndKept() {
        MapStore store = new MapStore();
        Identity first = Identity.load(store, new SecureRandom());
        // The TV Pi's checks: an id of 16 to 64 and a token of 32 to 128 URL-safe characters
        assertTrue(first.id.matches("[0-9a-f]{32}"));
        assertTrue(first.token.matches("[A-Za-z0-9_-]{43}"));
        Identity again = Identity.load(store, new SecureRandom());
        assertEquals(first.id, again.id);
        assertEquals(first.token, again.token);
        assertEquals(2, store.writes);
    }

    @Test public void aBrokenOneIsMadeAgain() {
        MapStore store = new MapStore();
        store.put(Identity.ID, "not an id");
        store.put(Identity.TOKEN, "short");
        Identity identity = Identity.load(store, new SecureRandom());
        assertTrue(identity.id.matches("[0-9a-f]{32}"));
        assertEquals(identity.token, store.get(Identity.TOKEN));
    }

    @Test public void eachPanelHasItsOwn() {
        assertNotEquals(Identity.load(new MapStore(), new SecureRandom()).token, Identity.load(new MapStore(), new SecureRandom()).token);
    }
}
