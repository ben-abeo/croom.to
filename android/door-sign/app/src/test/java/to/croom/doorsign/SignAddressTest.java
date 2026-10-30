package to.croom.doorsign;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertNull;
import static org.junit.Assert.assertTrue;

import org.junit.Test;

public class SignAddressTest {

    @Test public void anAddressAndPortIsTheSignOnThatServer() {
        assertEquals("http://10.0.0.5:8080/sign", SignAddress.normalize("10.0.0.5:8080"));
        assertEquals("http://10.0.0.5:8080/sign", SignAddress.normalize("  http://10.0.0.5:8080/  "));
        assertEquals("http://croom-1.local:8080/sign", SignAddress.normalize("croom-1.local:8080"));
    }

    @Test public void aFullAddressIsKept() {
        assertEquals("https://rooms.example.com/sign", SignAddress.normalize("https://rooms.example.com/sign"));
        assertEquals("http://10.0.0.5:8080/sign?room=2", SignAddress.normalize("http://10.0.0.5:8080/sign?room=2"));
    }

    @Test public void whatIsNotAWebAddressIsRefused() {
        assertNull(SignAddress.normalize(""));
        assertNull(SignAddress.normalize(null));
        assertNull(SignAddress.normalize("file:///sdcard/sign.html"));
        assertNull(SignAddress.normalize("javascript:alert(1)"));
        assertNull(SignAddress.normalize("http://"));
        assertNull(SignAddress.normalize("http://has space:8080"));
    }

    @Test public void thePageMayGoOnlyWhereItsServerIs() {
        String sign = "http://10.0.0.5:8080/sign";
        assertTrue(SignAddress.sameServer(sign, "http://10.0.0.5:8080/"));
        assertTrue(SignAddress.sameServer(sign, "http://10.0.0.5:8080/static/sign.css"));
        assertFalse(SignAddress.sameServer(sign, "http://10.0.0.5:9090/"));
        assertFalse(SignAddress.sameServer(sign, "https://10.0.0.5:8080/"));
        assertFalse(SignAddress.sameServer(sign, "http://evil.example.com/"));
        assertFalse(SignAddress.sameServer(sign, "not a url"));
    }

    @Test public void theRoomsAddressIsTheSignsServer() {
        assertEquals("http://10.0.0.5:8080", SignAddress.server("http://10.0.0.5:8080/sign"));
        assertEquals("https://rooms.example.com", SignAddress.server("https://rooms.example.com/sign?room=2"));
        assertNull(SignAddress.server(null));
        assertNull(SignAddress.server("not a url"));
    }
}
