package to.croom.doorsign;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertTrue;

import java.io.ByteArrayInputStream;
import java.io.ByteArrayOutputStream;
import java.nio.charset.StandardCharsets;
import java.util.Base64;
import org.junit.Test;

public class RtspTest {

    static final String PATH = Onvif.streamPath("secret-pw1");
    static final String URI = "rtsp://192.168.0.194:8554" + PATH;
    static final byte[] SPS = {0x67, 0x4d, 0x00, 0x1f, (byte) 0x95, (byte) 0xa8};
    static final byte[] PPS = {0x68, (byte) 0xee, 0x3c, (byte) 0x80};
    static final String SDP = Rtsp.sdp("192.168.0.194", SPS, PPS, null);
    static final byte[] AAC_CONFIG = {0x14, 0x08};                        // AAC-LC, 16 kHz, mono

    static Rtsp.Request request(String text) throws Exception {
        return Rtsp.read(new ByteArrayInputStream(text.getBytes(StandardCharsets.ISO_8859_1)));
    }

    static Rtsp.Reply ask(String method, String uri, String more) throws Exception {
        return Rtsp.answer(request(method + " " + uri + " RTSP/1.0\r\nCSeq: 7\r\n" + more + "\r\n"), PATH, SDP, "1A2B3C4D");
    }

    @Test public void optionsDescribeSetupPlayTeardown() throws Exception {
        Rtsp.Reply options = ask("OPTIONS", URI, "");
        assertEquals(200, options.status);
        assertEquals("7", options.headers.get("CSeq"));
        assertEquals("OPTIONS, DESCRIBE, SETUP, PLAY, TEARDOWN, GET_PARAMETER", options.headers.get("Public"));

        Rtsp.Reply describe = ask("DESCRIBE", URI, "Accept: application/sdp\r\n");
        assertEquals(200, describe.status);
        assertEquals("application/sdp", describe.headers.get("Content-Type"));
        assertEquals(URI + "/", describe.headers.get("Content-Base"));
        assertEquals(SDP, describe.body);

        Rtsp.Reply setup = ask("SETUP", URI + "/trackID=0", "Transport: RTP/AVP/TCP;unicast;interleaved=0-1\r\n");
        assertEquals(200, setup.status);
        assertEquals("RTP/AVP/TCP;unicast;interleaved=0-1", setup.headers.get("Transport"));
        assertEquals("1A2B3C4D;timeout=60", setup.headers.get("Session"));
        assertFalse(setup.play);

        Rtsp.Reply play = ask("PLAY", URI + "/", "Session: 1A2B3C4D\r\nRange: npt=0.000-\r\n");
        assertEquals(200, play.status);
        assertTrue(play.play);

        Rtsp.Reply keepAlive = ask("GET_PARAMETER", URI + "/", "Session: 1A2B3C4D\r\n");
        assertEquals(200, keepAlive.status);

        Rtsp.Reply teardown = ask("TEARDOWN", URI + "/", "Session: 1A2B3C4D\r\n");
        assertEquals(200, teardown.status);
        assertTrue(teardown.close);
    }

    @Test public void theWrongPathIsNotFound() throws Exception {
        for (String uri : new String[] {"rtsp://192.168.0.194:8554/guess", "rtsp://192.168.0.194:8554/", "rtsp://192.168.0.194:8554" + PATH + "x",
                "rtsp://192.168.0.194:8554/x" + PATH}) {
            Rtsp.Reply reply = ask("DESCRIBE", uri, "");
            assertEquals(uri, 404, reply.status);
            assertFalse(reply.body.contains("sprop"));
            assertEquals(404, ask("SETUP", uri + "/trackID=0", "Transport: RTP/AVP/TCP;unicast;interleaved=0-1\r\n").status);
            assertFalse(ask("PLAY", uri, "").play);
        }
    }

    @Test public void aNewPasswordRetiresTheOldStreamPath() throws Exception {
        Rtsp.Request old = request("DESCRIBE " + URI + " RTSP/1.0\r\nCSeq: 2\r\n\r\n");
        assertEquals(404, Rtsp.answer(old, Onvif.streamPath("new-pass-9"), SDP, "1").status);
    }

    @Test public void udpIsRefusedSoClientsUseTcp() throws Exception {
        assertEquals(461, ask("SETUP", URI + "/trackID=0", "Transport: RTP/AVP;unicast;client_port=5000-5001\r\n").status);
    }

    @Test public void unknownMethodsAreNotAllowed() throws Exception {
        assertEquals(405, ask("RECORD", URI, "").status);
    }

    @Test public void interleavedFramesFromTheClientAreSkipped() throws Exception {
        ByteArrayOutputStream bytes = new ByteArrayOutputStream();
        bytes.write(new byte[] {'$', 1, 0, 3, 9, 9, 9});
        bytes.write("GET_PARAMETER ".concat(URI).concat(" RTSP/1.0\r\nCSeq: 9\r\n\r\n").getBytes(StandardCharsets.ISO_8859_1));
        Rtsp.Request request = Rtsp.read(new ByteArrayInputStream(bytes.toByteArray()));
        assertEquals("GET_PARAMETER", request.method);
        assertEquals("9", request.headers.get("cseq"));
    }

    @Test public void theReplyOnTheWire() throws Exception {
        String wire = new String(ask("DESCRIBE", URI, "").bytes(), StandardCharsets.UTF_8);
        assertTrue(wire.startsWith("RTSP/1.0 200 OK\r\n"));
        assertTrue(wire.contains("CSeq: 7\r\n"));
        assertTrue(wire.contains("Content-Length: " + SDP.getBytes(StandardCharsets.UTF_8).length + "\r\n"));
        assertTrue(wire.endsWith("\r\n\r\n" + SDP));
    }

    @Test public void withSoundTheSdpHasAnAacTrackAfterThePicture() {
        String sdp = Rtsp.sdp("192.168.0.194", SPS, PPS, AAC_CONFIG);
        assertTrue(sdp.startsWith(SDP));
        assertTrue(sdp.endsWith("m=audio 0 RTP/AVP 97\r\na=rtpmap:97 MPEG4-GENERIC/16000/1\r\n"
                + "a=fmtp:97 streamtype=5;profile-level-id=15;mode=AAC-hbr;sizelength=13;indexlength=3;indexdeltalength=3;config=1408\r\n"
                + "a=control:trackID=1\r\n"));
        assertFalse(SDP.contains("m=audio"));
    }

    @Test public void theSoundTrackIsSetUpOnChannelsTwoAndThree() throws Exception {
        Rtsp.Reply sound = ask("SETUP", URI + "/trackID=1", "Transport: RTP/AVP/TCP;unicast;interleaved=2-3\r\nSession: 1A2B3C4D\r\n");
        assertEquals(200, sound.status);
        assertEquals("RTP/AVP/TCP;unicast;interleaved=2-3", sound.headers.get("Transport"));
        assertTrue(sound.sound);
        assertFalse(ask("SETUP", URI + "/trackID=0", "Transport: RTP/AVP/TCP;unicast;interleaved=0-1\r\n").sound);
    }

    @Test public void sdpHasTheParameterSets() {
        assertTrue(SDP.contains("m=video 0 RTP/AVP 96\r\n"));
        assertTrue(SDP.contains("a=rtpmap:96 H264/90000\r\n"));
        assertTrue(SDP.contains("a=fmtp:96 packetization-mode=1;profile-level-id=4d001f;sprop-parameter-sets="
                + Base64.getEncoder().encodeToString(SPS) + "," + Base64.getEncoder().encodeToString(PPS) + "\r\n"));
        assertTrue(SDP.contains("a=control:trackID=0\r\n"));
        assertTrue(SDP.startsWith("v=0\r\n"));
    }
}
