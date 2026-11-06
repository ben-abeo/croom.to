package to.croom.doorsign;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertNotEquals;
import static org.junit.Assert.assertTrue;

import java.io.ByteArrayInputStream;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.time.Duration;
import java.time.Instant;
import java.util.Base64;
import javax.xml.parsers.DocumentBuilderFactory;
import org.junit.Test;

public class OnvifTest {

    static final Instant NOW = Instant.parse("2026-11-06T18:00:00Z");
    static final CameraLogin LOGIN = new CameraLogin("camera", "secret-pw1", "Conference Room B door sign");
    static final Onvif.Info INFO = new Onvif.Info(LOGIN, "192.168.0.194", "f8:7a:39:7c:f6:25", "WF2489T", "3.0", "abc123");

    /** A request as Protect sends it: SOAP 1.2, the action in the Body, a WS-UsernameToken with a PasswordDigest (or none). */
    static String request(String action, String user, String password, Instant created) throws Exception {
        String security = "";
        if (user != null) {
            byte[] nonce = "0123456789abcdef".getBytes(StandardCharsets.UTF_8);
            String when = created.toString();
            MessageDigest sha1 = MessageDigest.getInstance("SHA-1");
            sha1.update(nonce);
            sha1.update(when.getBytes(StandardCharsets.UTF_8));
            sha1.update(password.getBytes(StandardCharsets.UTF_8));
            String digest = Base64.getEncoder().encodeToString(sha1.digest());
            security = "<s:Header><wsse:Security xmlns:wsse=\"http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd\" "
                    + "xmlns:wsu=\"http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd\"><wsse:UsernameToken>"
                    + "<wsse:Username>" + user + "</wsse:Username>"
                    + "<wsse:Password Type=\"http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-username-token-profile-1.0#PasswordDigest\">"
                    + digest + "</wsse:Password>"
                    + "<wsse:Nonce EncodingType=\"http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-soap-message-security-1.0#Base64Binary\">"
                    + Base64.getEncoder().encodeToString(nonce) + "</wsse:Nonce>"
                    + "<wsu:Created>" + when + "</wsu:Created></wsse:UsernameToken></wsse:Security></s:Header>";
        }
        return "<?xml version=\"1.0\" encoding=\"UTF-8\"?><s:Envelope xmlns:s=\"http://www.w3.org/2003/05/soap-envelope\" "
                + "xmlns:trt=\"http://www.onvif.org/ver10/media/wsdl\" xmlns:tds=\"http://www.onvif.org/ver10/device/wsdl\">" + security
                + "<s:Body><" + action + "/></s:Body></s:Envelope>";
    }

    static Onvif.Reply ask(String action) throws Exception {
        return Onvif.handle(request(action, "camera", "secret-pw1", NOW), INFO, NOW);
    }

    static void wellFormed(String xml) throws Exception {
        DocumentBuilderFactory factory = DocumentBuilderFactory.newInstance();
        factory.setNamespaceAware(true);
        factory.newDocumentBuilder().parse(new ByteArrayInputStream(xml.getBytes(StandardCharsets.UTF_8)));
    }

    @Test public void dateAndTimeNeedsNoLogin() throws Exception {
        Onvif.Reply reply = Onvif.handle(request("tds:GetSystemDateAndTime", null, null, NOW), INFO, NOW);
        assertEquals(200, reply.status);
        assertTrue(reply.xml.contains("<tt:Year>2026</tt:Year>") && reply.xml.contains("<tt:Hour>18</tt:Hour>"));
        wellFormed(reply.xml);
    }

    @Test public void everythingElseNeedsTheLogin() throws Exception {
        Onvif.Reply reply = Onvif.handle(request("trt:GetProfiles", null, null, NOW), INFO, NOW);
        assertEquals(401, reply.status);
        assertTrue(reply.xml.contains("ter:NotAuthorized"));
        assertFalse(reply.xml.contains("rtsp://"));
        wellFormed(reply.xml);
    }

    @Test public void digestLoginIsAccepted() throws Exception {
        assertEquals(200, ask("trt:GetProfiles").status);
    }

    @Test public void wrongPasswordOrUserIsRefused() throws Exception {
        assertEquals(401, Onvif.handle(request("trt:GetStreamUri", "camera", "guess-pw1", NOW), INFO, NOW).status);
        assertEquals(401, Onvif.handle(request("trt:GetStreamUri", "admin", "secret-pw1", NOW), INFO, NOW).status);
    }

    @Test public void plainTextPasswordIsAccepted() throws Exception {
        String text = request("trt:GetProfiles", "camera", "secret-pw1", NOW)
                .replaceAll("<wsse:Password Type=\"[^\"]*\">[^<]*</wsse:Password>",
                        "<wsse:Password Type=\"http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-username-token-profile-1.0#PasswordText\">"
                                + "secret-pw1</wsse:Password>");
        assertEquals(200, Onvif.handle(text, INFO, NOW).status);
    }

    @Test public void createdFarFromNowIsRefused() throws Exception {
        assertEquals(401, Onvif.handle(request("trt:GetProfiles", "camera", "secret-pw1", NOW.minus(Duration.ofMinutes(16))), INFO, NOW).status);
        assertEquals(200, Onvif.handle(request("trt:GetProfiles", "camera", "secret-pw1", NOW.minus(Duration.ofMinutes(14))), INFO, NOW).status);
        assertEquals(200, Onvif.handle(request("trt:GetProfiles", "camera", "secret-pw1", NOW.plus(Duration.ofMinutes(14))), INFO, NOW).status);
    }

    @Test public void passwordsWithXmlCharactersWork() throws Exception {
        CameraLogin login = new CameraLogin("camera", "p&ss<w>rd1\"", "Room <B> & co");
        Onvif.Info info = new Onvif.Info(login, "192.168.0.194", "f8:7a:39:7c:f6:25", "WF2489T", "3.0", "abc123");
        // The password travels digested, so it is never in the XML; the name is, escaped
        for (String action : new String[] {"trt:GetStreamUri", "tds:GetDeviceInformation", "tds:GetScopes"}) {
            Onvif.Reply reply = Onvif.handle(request(action, "camera", login.password, NOW), info, NOW);
            assertEquals(action, 200, reply.status);
            wellFormed(reply.xml);
        }
    }

    @Test public void theStreamUriCarriesThePasswordsPath() throws Exception {
        Onvif.Reply reply = ask("trt:GetStreamUri");
        assertTrue(reply.xml.contains("<tt:Uri>rtsp://192.168.0.194:8554" + Onvif.streamPath("secret-pw1") + "</tt:Uri>"));
        wellFormed(reply.xml);
    }

    @Test public void aNewPasswordGivesANewPath() {
        String a = Onvif.streamPath("secret-pw1");
        String b = Onvif.streamPath("secret-pw2");
        assertNotEquals(a, b);
        assertEquals(33, a.length());
        assertTrue(a.matches("/[0-9a-f]{32}"));
        assertEquals(a, Onvif.streamPath("secret-pw1"));
    }

    @Test public void profilesHaveRateControlAndVideoSourceConfiguration() throws Exception {
        for (String action : new String[] {"trt:GetProfiles", "trt:GetProfile"}) {
            Onvif.Reply reply = ask(action);
            assertEquals(200, reply.status);
            for (String needle : new String[] {"token=\"main\"", "<tt:VideoSourceConfiguration token=\"vsc\"", "<tt:SourceToken>vs</tt:SourceToken>",
                    "width=\"1280\" height=\"720\"", "<tt:VideoEncoderConfiguration token=\"vec\"", "<tt:Encoding>H264</tt:Encoding>",
                    "<tt:FrameRateLimit>15</tt:FrameRateLimit>", "<tt:EncodingInterval>1</tt:EncodingInterval>",
                    "<tt:BitrateLimit>1500</tt:BitrateLimit>", "<tt:GovLength>30</tt:GovLength>", "<tt:H264Profile>Main</tt:H264Profile>"}) {
                assertTrue(action + " " + needle, reply.xml.contains(needle));
            }
            wellFormed(reply.xml);
        }
    }

    @Test public void theDeviceAndItsServices() throws Exception {
        Onvif.Reply info = ask("tds:GetDeviceInformation");
        assertTrue(info.xml.contains("<tds:Manufacturer>Crystal Meet</tds:Manufacturer>"));
        assertTrue(info.xml.contains("<tds:Model>Conference Room B door sign</tds:Model>"));
        assertTrue(info.xml.contains("<tds:HardwareId>WF2489T</tds:HardwareId>"));
        for (String action : new String[] {"tds:GetCapabilities", "tds:GetServices"}) {
            Onvif.Reply reply = ask(action);
            assertTrue(action, reply.xml.contains("http://192.168.0.194:8000/onvif/device_service"));
            assertTrue(action, reply.xml.contains("http://192.168.0.194:8000/onvif/media_service"));
            wellFormed(reply.xml);
        }
        for (String action : new String[] {"tds:GetHostname", "tds:GetScopes", "trt:GetVideoSources", "trt:GetVideoSourceConfigurations",
                "trt:GetVideoEncoderConfigurations"}) {
            Onvif.Reply reply = ask(action);
            assertEquals(action, 200, reply.status);
            wellFormed(reply.xml);
        }
    }

    @Test public void networkInterfacesGiveTheMac() throws Exception {
        Onvif.Reply reply = ask("tds:GetNetworkInterfaces");
        assertTrue(reply.xml.contains("<tt:HwAddress>f8:7a:39:7c:f6:25</tt:HwAddress>"));
        wellFormed(reply.xml);
    }

    @Test public void unknownActionsAreNotSupported() throws Exception {
        Onvif.Reply reply = ask("trt:GetAudioOutputs");
        assertEquals(400, reply.status);
        assertEquals("GetAudioOutputs", reply.action);
        assertTrue(reply.xml.contains("ter:ActionNotSupported"));
        wellFormed(reply.xml);
    }

    @Test public void aBodyThatIsNotSoapIsRefused() {
        assertEquals(400, Onvif.handle("hello", INFO, NOW).status);
    }
}
