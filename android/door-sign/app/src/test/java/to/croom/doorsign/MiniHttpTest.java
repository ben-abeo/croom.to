package to.croom.doorsign;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertTrue;
import static org.junit.Assert.fail;

import java.io.ByteArrayInputStream;
import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import org.junit.Test;

public class MiniHttpTest {

    static ByteArrayInputStream in(String text) {
        return new ByteArrayInputStream(text.getBytes(StandardCharsets.UTF_8));
    }

    @Test public void aPostWithItsBody() throws Exception {
        MiniHttp.Request request = MiniHttp.read(in("POST /onvif/device_service HTTP/1.1\r\nHost: x\r\nContent-Type: application/soap+xml\r\n"
                + "Content-Length: 5\r\n\r\nhelloEXTRA"));
        assertEquals("POST", request.method);
        assertEquals("/onvif/device_service", request.path);
        assertEquals("application/soap+xml", request.headers.get("content-type"));
        assertEquals("hello", request.body);
    }

    @Test public void tooLargeABodyIsRefused() {
        try {
            MiniHttp.read(in("POST / HTTP/1.1\r\nContent-Length: " + (70 * 1024) + "\r\n\r\n"));
            fail();
        } catch (IOException expected) {
        }
    }

    @Test public void tooManyHeadersOrAnEndlessLineAreRefused() {
        StringBuilder many = new StringBuilder("GET / HTTP/1.1\r\n");
        for (int i = 0; i < 200; i++) many.append("X-").append(i).append(": y\r\n");
        for (String text : new String[] {many + "\r\n", "GET /" + "a".repeat(10_000) + " HTTP/1.1\r\n\r\n", "", "GET\r\n\r\n"}) {
            try {
                MiniHttp.read(in(text));
                fail(text.length() > 40 ? text.substring(0, 40) : text);
            } catch (IOException expected) {
            }
        }
    }

    @Test public void theAnswer() throws Exception {
        ByteArrayOutputStream out = new ByteArrayOutputStream();
        MiniHttp.write(out, 401, "application/soap+xml; charset=utf-8", "<x/>é");
        String text = out.toString(StandardCharsets.UTF_8);
        assertTrue(text.startsWith("HTTP/1.1 401 Unauthorized\r\n"));
        assertTrue(text.contains("Content-Length: 6\r\n"));       // bytes (é is two), not characters (5)
        assertTrue(text.contains("Connection: close\r\n"));
        assertTrue(text.endsWith("\r\n\r\n<x/>é"));
    }
}
