package to.croom.doorsign;

import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.util.Map;

/** Agent.Http with HttpURLConnection: the check-in, the APK download and the reach test, to the room's TV Pi on the LAN. */
final class UrlHttp implements Agent.Http {

    static final int CONNECT_MS = 5_000;
    static final int READ_MS = 10_000;
    static final int DOWNLOAD_READ_MS = 120_000;     // the TV Pi may still be fetching the APK from GitHub
    static final int MAX_ANSWER = 64 * 1024;

    @Override public Agent.Response post(String url, Map<String, String> headers, byte[] body) throws IOException {
        HttpURLConnection connection = open(url, headers, READ_MS);
        try {
            connection.setRequestMethod("POST");
            connection.setDoOutput(true);
            connection.setFixedLengthStreamingMode(body.length);
            try (OutputStream out = connection.getOutputStream()) {
                out.write(body);
            }
            int status = connection.getResponseCode();
            InputStream in = status >= 400 ? connection.getErrorStream() : connection.getInputStream();
            return new Agent.Response(status, in == null ? "" : read(in));
        } finally {
            connection.disconnect();
        }
    }

    @Override public int download(String url, Map<String, String> headers, File file, long maxBytes) throws IOException {
        HttpURLConnection connection = open(url, headers, DOWNLOAD_READ_MS);
        try {
            int status = connection.getResponseCode();
            if (status != 200) return status;
            long total = 0;
            try (InputStream in = connection.getInputStream(); FileOutputStream out = new FileOutputStream(file)) {
                byte[] buffer = new byte[64 * 1024];
                for (int n; (n = in.read(buffer)) > 0; ) {
                    total += n;
                    if (total > maxBytes) throw new IOException("the download is larger than the TV Pi said");
                    out.write(buffer, 0, n);
                }
                out.getFD().sync();
            }
            return status;
        } finally {
            connection.disconnect();
        }
    }

    @Override public boolean answers(String url) {
        try {
            HttpURLConnection connection = open(url, java.util.Collections.emptyMap(), 4_000);
            try {
                connection.setConnectTimeout(4_000);
                return connection.getResponseCode() == 200;
            } finally {
                connection.disconnect();
            }
        } catch (IOException e) {
            return false;
        }
    }

    private static HttpURLConnection open(String url, Map<String, String> headers, int readMs) throws IOException {
        HttpURLConnection connection = (HttpURLConnection) new URL(url).openConnection();
        connection.setConnectTimeout(CONNECT_MS);
        connection.setReadTimeout(readMs);
        connection.setUseCaches(false);
        connection.setInstanceFollowRedirects(false);    // the TV Pi answers itself; a redirect is not followed off it
        for (Map.Entry<String, String> header : headers.entrySet()) connection.setRequestProperty(header.getKey(), header.getValue());
        return connection;
    }

    private static String read(InputStream in) throws IOException {
        try (InputStream stream = in) {
            ByteArrayOutputStream bytes = new ByteArrayOutputStream();
            byte[] buffer = new byte[8192];
            for (int n; (n = stream.read(buffer)) > 0; ) {
                if (bytes.size() + n > MAX_ANSWER) throw new IOException("the answer is too long");
                bytes.write(buffer, 0, n);
            }
            return bytes.toString(StandardCharsets.UTF_8.name());
        }
    }
}
