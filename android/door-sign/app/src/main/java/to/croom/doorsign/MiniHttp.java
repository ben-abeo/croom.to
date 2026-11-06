package to.croom.doorsign;

import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.nio.charset.StandardCharsets;
import java.util.HashMap;
import java.util.Map;

/**
 * Just enough HTTP/1.1 for the camera's ONVIF answers (spec 2026-11-06): one request per connection, a body by Content-Length, and
 * limits on every part, since anyone on the LAN can connect. The RTSP server reads its requests with the same lines.
 */
final class MiniHttp {

    static final int MAX_LINE = 8192;
    static final int MAX_HEADERS = 100;
    static final int MAX_BODY = 64 * 1024;

    private MiniHttp() {}

    static final class Request {
        final String method;
        final String path;
        final Map<String, String> headers;   // names in lower case
        final String body;

        Request(String method, String path, Map<String, String> headers, String body) {
            this.method = method;
            this.path = path;
            this.headers = headers;
            this.body = body;
        }
    }

    static Request read(InputStream in) throws IOException {
        String[] start = line(in).split(" ");
        if (start.length != 3 || start[0].isEmpty() || !start[1].startsWith("/")) throw new IOException("Not an HTTP request");
        Map<String, String> headers = headers(in);
        int length = contentLength(headers);
        if (length > MAX_BODY) throw new IOException("Body too large: " + length);
        byte[] body = new byte[length];
        new java.io.DataInputStream(in).readFully(body);     // not readNBytes: Android has it from 13 only, the app runs from 8
        return new Request(start[0], start[1], headers, new String(body, StandardCharsets.UTF_8));
    }

    static void write(OutputStream out, int status, String contentType, String body) throws IOException {
        byte[] bytes = body.getBytes(StandardCharsets.UTF_8);
        String head = "HTTP/1.1 " + status + " " + reason(status) + "\r\nContent-Type: " + contentType + "\r\nContent-Length: " + bytes.length
                + "\r\nConnection: close\r\n\r\n";
        out.write(head.getBytes(StandardCharsets.ISO_8859_1));
        out.write(bytes);
        out.flush();
    }

    /** One CRLF- (or LF-) ended line, without its ending; IOException at the end of the stream or past MAX_LINE. */
    static String line(InputStream in) throws IOException {
        ByteArrayOutputStream line = new ByteArrayOutputStream();
        while (true) {
            int b = in.read();
            if (b < 0) throw new IOException("Connection closed");
            if (b == '\n') break;
            if (line.size() >= MAX_LINE) throw new IOException("Line too long");
            line.write(b);
        }
        String text = new String(line.toByteArray(), StandardCharsets.ISO_8859_1);
        return text.endsWith("\r") ? text.substring(0, text.length() - 1) : text;
    }

    /** Header lines up to the blank one, names in lower case. */
    static Map<String, String> headers(InputStream in) throws IOException {
        Map<String, String> headers = new HashMap<>();
        for (String line = line(in); !line.isEmpty(); line = line(in)) {
            if (headers.size() >= MAX_HEADERS) throw new IOException("Too many headers");
            int colon = line.indexOf(':');
            if (colon > 0) headers.put(line.substring(0, colon).trim().toLowerCase(java.util.Locale.ROOT), line.substring(colon + 1).trim());
        }
        return headers;
    }

    static int contentLength(Map<String, String> headers) throws IOException {
        String value = headers.get("content-length");
        if (value == null) return 0;
        try {
            int length = Integer.parseInt(value);
            if (length < 0) throw new IOException("Bad Content-Length");
            return length;
        } catch (NumberFormatException e) {
            throw new IOException("Bad Content-Length");
        }
    }

    static String reason(int status) {
        switch (status) {
            case 200: return "OK";
            case 400: return "Bad Request";
            case 401: return "Unauthorized";
            case 404: return "Not Found";
            case 405: return "Method Not Allowed";
            case 454: return "Session Not Found";
            case 461: return "Unsupported Transport";
            default: return "Error";
        }
    }
}
