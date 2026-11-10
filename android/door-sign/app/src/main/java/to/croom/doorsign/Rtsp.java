package to.croom.doorsign;

import java.io.ByteArrayOutputStream;
import java.io.DataInputStream;
import java.io.IOException;
import java.io.InputStream;
import java.nio.charset.StandardCharsets;
import java.util.Base64;
import java.util.LinkedHashMap;
import java.util.Map;

/**
 * The camera's RTSP sessions (spec 2026-11-06, section 5): one H.264 track (trackID=0, channels 0-1) and, while the room records
 * sound, one AAC track (trackID=1, channels 2-3; spec 2026-11-10), sent interleaved on the RTSP connection itself (TCP), as UniFi
 * Protect asks for it. Only the stream's own path, which Onvif.streamPath derives from the room's password and only an
 * authenticated GetStreamUri hands out, is answered; any other path is not found.
 */
final class Rtsp {

    static final String PUBLIC = "OPTIONS, DESCRIBE, SETUP, PLAY, TEARDOWN, GET_PARAMETER";

    private Rtsp() {}

    static final class Request {
        final String method;
        final String uri;
        final Map<String, String> headers;   // names in lower case

        Request(String method, String uri, Map<String, String> headers) {
            this.method = method;
            this.uri = uri;
            this.headers = headers;
        }
    }

    static final class Reply {
        final int status;
        final Map<String, String> headers = new LinkedHashMap<>();
        String body = "";
        boolean play;      // start sending the stream on this connection
        boolean sound;     // this SETUP was the sound's track
        boolean close;     // end the connection

        Reply(int status, String cseq) {
            this.status = status;
            if (cseq != null) headers.put("CSeq", cseq);
        }

        byte[] bytes() {
            byte[] content = body.getBytes(StandardCharsets.UTF_8);
            StringBuilder head = new StringBuilder("RTSP/1.0 ").append(status).append(' ').append(MiniHttp.reason(status)).append("\r\n");
            for (Map.Entry<String, String> h : headers.entrySet()) head.append(h.getKey()).append(": ").append(h.getValue()).append("\r\n");
            if (content.length > 0) head.append("Content-Length: ").append(content.length).append("\r\n");
            head.append("\r\n");
            ByteArrayOutputStream out = new ByteArrayOutputStream();
            byte[] start = head.toString().getBytes(StandardCharsets.ISO_8859_1);
            out.write(start, 0, start.length);
            out.write(content, 0, content.length);
            return out.toByteArray();
        }
    }

    /** The next request on the connection; interleaved frames the client sends (its RTCP reports) are read and dropped. */
    static Request read(InputStream in) throws IOException {
        DataInputStream data = new DataInputStream(in);
        while (true) {
            int first = data.read();
            if (first < 0) throw new IOException("Connection closed");
            if (first == '\r' || first == '\n') continue;     // a blank line between requests
            if (first != '$') {
                String line = (char) first + MiniHttp.line(data);
                String[] start = line.split(" ");
                if (start.length != 3 || !start[2].startsWith("RTSP/")) throw new IOException("Not an RTSP request");
                Map<String, String> headers = MiniHttp.headers(data);
                int length = MiniHttp.contentLength(headers);
                if (length > MiniHttp.MAX_BODY) throw new IOException("Body too large");
                data.readFully(new byte[length]);
                return new Request(start[0], start[1], headers);
            }
            data.readUnsignedByte();                         // channel
            data.readFully(new byte[data.readUnsignedShort()]);   // not skipBytes: on a socket it may skip less
        }
    }

    static Reply answer(Request r, String streamPath, String sdp, String sessionId) {
        String cseq = r.headers.get("cseq");
        if (r.method.equals("OPTIONS")) {
            Reply reply = new Reply(200, cseq);
            reply.headers.put("Public", PUBLIC);
            return reply;
        }
        String path = path(r.uri);
        if (path == null || !(path.equals(streamPath) || path.startsWith(streamPath + "/"))) return new Reply(404, cseq);
        Reply reply;
        switch (r.method) {
            case "DESCRIBE":
                reply = new Reply(200, cseq);
                reply.headers.put("Content-Type", "application/sdp");
                reply.headers.put("Content-Base", r.uri.endsWith("/") ? r.uri : r.uri + "/");
                reply.body = sdp;
                return reply;
            case "SETUP": {
                String transport = r.headers.getOrDefault("transport", "");
                if (!transport.contains("RTP/AVP/TCP")) return new Reply(461, cseq);   // ponytail: TCP only, as Protect asks; UDP if a client ever needs it
                reply = new Reply(200, cseq);
                reply.sound = path.endsWith("/trackID=1");
                reply.headers.put("Transport", "RTP/AVP/TCP;unicast;interleaved=" + (reply.sound ? "2-3" : "0-1"));
                reply.headers.put("Session", sessionId + ";timeout=60");
                return reply;
            }
            case "PLAY":
                reply = new Reply(200, cseq);
                reply.headers.put("Session", sessionId);
                reply.headers.put("Range", "npt=0.000-");
                reply.play = true;
                return reply;
            case "GET_PARAMETER":
            case "SET_PARAMETER":
                reply = new Reply(200, cseq);
                reply.headers.put("Session", sessionId);
                return reply;
            case "TEARDOWN":
                reply = new Reply(200, cseq);
                reply.close = true;
                return reply;
            default:
                reply = new Reply(405, cseq);
                reply.headers.put("Public", PUBLIC);
                return reply;
        }
    }

    /**
     * The SDP: the H.264 track, its parameter sets from the encoder's SPS and PPS (each without a start code), then the AAC track when
     * there is sound (`aacConfig`, the encoder's AudioSpecificConfig; null: picture only).
     */
    static String sdp(String host, byte[] sps, byte[] pps, byte[] aacConfig) {
        String profileLevel = String.format("%02x%02x%02x", sps[1] & 0xff, sps[2] & 0xff, sps[3] & 0xff);
        Base64.Encoder b64 = Base64.getEncoder();
        return "v=0\r\no=- 0 0 IN IP4 " + host + "\r\ns=Door sign\r\nc=IN IP4 0.0.0.0\r\nt=0 0\r\na=control:*\r\n"
                + "m=video 0 RTP/AVP 96\r\na=rtpmap:96 H264/90000\r\n"
                + "a=fmtp:96 packetization-mode=1;profile-level-id=" + profileLevel + ";sprop-parameter-sets=" + b64.encodeToString(sps) + ","
                + b64.encodeToString(pps) + "\r\na=control:trackID=0\r\n" + (aacConfig == null ? "" : sound(aacConfig));
    }

    private static String sound(byte[] aacConfig) {
        StringBuilder config = new StringBuilder();
        for (byte b : aacConfig) config.append(String.format("%02x", b & 0xff));
        return "m=audio 0 RTP/AVP " + AacRtp.PAYLOAD_TYPE + "\r\na=rtpmap:" + AacRtp.PAYLOAD_TYPE + " MPEG4-GENERIC/" + Onvif.SAMPLE_RATE + "/1\r\n"
                + "a=fmtp:" + AacRtp.PAYLOAD_TYPE + " streamtype=5;profile-level-id=15;mode=AAC-hbr;sizelength=13;indexlength=3;indexdeltalength=3;"
                + "config=" + config + "\r\na=control:trackID=1\r\n";
    }

    /** The path of an rtsp:// URI ("rtsp://host:8554/abc/trackID=0" → "/abc/trackID=0"), or null. */
    private static String path(String uri) {
        if (!uri.startsWith("rtsp://")) return null;
        int slash = uri.indexOf('/', "rtsp://".length());
        return slash < 0 ? "/" : uri.substring(slash);
    }
}
