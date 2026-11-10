package to.croom.doorsign;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.time.Duration;
import java.time.Instant;
import java.time.ZoneOffset;
import java.time.ZonedDateTime;
import java.time.format.DateTimeParseException;
import java.util.Base64;
import java.util.regex.Matcher;
import java.util.regex.Pattern;
import javax.crypto.Mac;
import javax.crypto.spec.SecretKeySpec;

/**
 * The camera's ONVIF answers (spec 2026-11-06, section 5): the least UniFi Protect needs to adopt one camera with one H.264 stream.
 * GetSystemDateAndTime is open (Protect asks it first, to sign its requests with the camera's clock); everything else needs the
 * room's login as a WS-UsernameToken. The stream's RTSP path is derived from the password and only GetStreamUri hands it out, so the
 * stream is closed to anyone without the password whether or not Protect also logs in on RTSP.
 *
 * Requests are read with patterns, not an XML parser: Android's and the JVM's parsers differ, and only a few elements matter.
 */
final class Onvif {

    static final int PORT = 8000;
    static final int RTSP_PORT = 8554;
    static final int WIDTH = 1280;
    static final int HEIGHT = 720;
    static final int FPS = 15;
    static final int BITRATE_KBPS = 1500;
    static final int GOP = 30;          // a keyframe every 2 seconds at 15 frames a second
    static final int SAMPLE_RATE = 16000;           // the sound (spec 2026-11-10): AAC-LC, mono, as IP cameras send it
    static final int SOUND_BITRATE_KBPS = 32;
    static final Duration CLOCK_SKEW = Duration.ofMinutes(15);

    private static final Pattern BODY_ACTION = Pattern.compile("<(?:[\\w.-]+:)?Body\\b[^>]*>\\s*<(?:[\\w.-]+:)?([\\w.-]+)");
    private static final Pattern USERNAME = element("Username");
    private static final Pattern PASSWORD = Pattern.compile("<(?:[\\w.-]+:)?Password\\b([^>]*)>([^<]*)<", Pattern.DOTALL);
    private static final Pattern NONCE = element("Nonce");
    private static final Pattern CREATED = element("Created");

    private Onvif() {}

    static final class Info {
        final CameraLogin login;
        final String host;
        final String mac;
        final String model;
        final String firmware;
        final String serial;

        Info(CameraLogin login, String host, String mac, String model, String firmware, String serial) {
            this.login = login;
            this.host = host;
            this.mac = mac;
            this.model = model;
            this.firmware = firmware;
            this.serial = serial;
        }
    }

    static final class Reply {
        final int status;
        final String xml;
        final String action;    // the action asked for ("" when there was none), for the log

        Reply(int status, String xml, String action) {
            this.status = status;
            this.xml = xml;
            this.action = action;
        }
    }

    static Reply handle(String soapBody, Info info, Instant now) {
        Matcher m = BODY_ACTION.matcher(soapBody);
        if (!m.find()) return new Reply(400, fault("s:Sender", "ter:InvalidArgVal", "Not a SOAP request"), "");
        String action = m.group(1);
        if (!action.equals("GetSystemDateAndTime") && !authorized(soapBody, info.login, now)) {
            return new Reply(401, fault("s:Sender", "ter:NotAuthorized", "Sender not authorized"), action);
        }
        String body = answer(action, info, now);
        if (body == null) return new Reply(400, fault("s:Receiver", "ter:ActionNotSupported", "Optional action not implemented"), action);
        return new Reply(200, envelope(body), action);
    }

    /** Whether the request carries the room's login as a WS-UsernameToken (PasswordDigest or PasswordText), made within CLOCK_SKEW. */
    static boolean authorized(String soapBody, CameraLogin login, Instant now) {
        if (login == null) return false;
        String user = first(USERNAME, soapBody);
        Matcher password = PASSWORD.matcher(soapBody);
        if (user == null || !password.find() || !constantEquals(unescape(user.trim()), login.user)) return false;
        String created = first(CREATED, soapBody);
        if (created != null) {
            try {
                Instant when = Instant.parse(created.trim());
                if (Duration.between(when, now).abs().compareTo(CLOCK_SKEW) > 0) return false;
            } catch (DateTimeParseException e) {
                return false;
            }
        }
        String given = unescape(password.group(2).trim());
        if (password.group(1).contains("#PasswordText")) return constantEquals(given, login.password);
        try {
            String nonce = first(NONCE, soapBody);
            MessageDigest sha1 = MessageDigest.getInstance("SHA-1");
            if (nonce != null) sha1.update(Base64.getDecoder().decode(nonce.trim()));
            if (created != null) sha1.update(created.trim().getBytes(StandardCharsets.UTF_8));
            sha1.update(login.password.getBytes(StandardCharsets.UTF_8));
            return MessageDigest.isEqual(Base64.getEncoder().encode(sha1.digest()), given.getBytes(StandardCharsets.UTF_8));
        } catch (Exception e) {
            return false;
        }
    }

    /** The stream's RTSP path: "/" and 32 hex digits of HMAC-SHA256(password, "door-sign-stream"). A new password, a new path. */
    static String streamPath(String password) {
        try {
            Mac mac = Mac.getInstance("HmacSHA256");
            mac.init(new SecretKeySpec(password.getBytes(StandardCharsets.UTF_8), "HmacSHA256"));
            byte[] sum = mac.doFinal("door-sign-stream".getBytes(StandardCharsets.UTF_8));
            StringBuilder path = new StringBuilder("/");
            for (int i = 0; i < 16; i++) path.append(String.format("%02x", sum[i]));
            return path.toString();
        } catch (Exception e) {
            throw new IllegalStateException(e);
        }
    }

    private static String answer(String action, Info info, Instant now) {
        String device = "http://" + info.host + ":" + PORT + "/onvif/device_service";
        String media = "http://" + info.host + ":" + PORT + "/onvif/media_service";
        switch (action) {
            case "GetSystemDateAndTime": {
                ZonedDateTime t = now.atZone(ZoneOffset.UTC);
                return "<tds:GetSystemDateAndTimeResponse><tds:SystemDateAndTime><tt:DateTimeType>NTP</tt:DateTimeType>"
                        + "<tt:DaylightSavings>false</tt:DaylightSavings><tt:TimeZone><tt:TZ>UTC0</tt:TZ></tt:TimeZone><tt:UTCDateTime>"
                        + "<tt:Time><tt:Hour>" + t.getHour() + "</tt:Hour><tt:Minute>" + t.getMinute() + "</tt:Minute><tt:Second>" + t.getSecond()
                        + "</tt:Second></tt:Time><tt:Date><tt:Year>" + t.getYear() + "</tt:Year><tt:Month>" + t.getMonthValue() + "</tt:Month><tt:Day>"
                        + t.getDayOfMonth() + "</tt:Day></tt:Date></tt:UTCDateTime></tds:SystemDateAndTime></tds:GetSystemDateAndTimeResponse>";
            }
            case "GetDeviceInformation":
                return "<tds:GetDeviceInformationResponse><tds:Manufacturer>Crystal Meet</tds:Manufacturer><tds:Model>" + esc(name(info))
                        + "</tds:Model><tds:FirmwareVersion>" + esc(info.firmware) + "</tds:FirmwareVersion><tds:SerialNumber>" + esc(info.serial)
                        + "</tds:SerialNumber><tds:HardwareId>" + esc(info.model) + "</tds:HardwareId></tds:GetDeviceInformationResponse>";
            case "GetCapabilities":
                return "<tds:GetCapabilitiesResponse><tds:Capabilities><tt:Device><tt:XAddr>" + device + "</tt:XAddr></tt:Device><tt:Media><tt:XAddr>"
                        + media + "</tt:XAddr><tt:StreamingCapabilities><tt:RTPMulticast>false</tt:RTPMulticast><tt:RTP_TCP>true</tt:RTP_TCP>"
                        + "<tt:RTP_RTSP_TCP>true</tt:RTP_RTSP_TCP></tt:StreamingCapabilities></tt:Media></tds:Capabilities></tds:GetCapabilitiesResponse>";
            case "GetServices":
                return "<tds:GetServicesResponse>" + service("http://www.onvif.org/ver10/device/wsdl", device)
                        + service("http://www.onvif.org/ver10/media/wsdl", media) + "</tds:GetServicesResponse>";
            case "GetNetworkInterfaces":
                // ponytail: the prefix length is not known here; 24 is what the office LAN has, and Protect only reads the address and MAC
                return "<tds:GetNetworkInterfacesResponse><tds:NetworkInterfaces token=\"eth0\"><tt:Enabled>true</tt:Enabled><tt:Info>"
                        + "<tt:Name>eth0</tt:Name><tt:HwAddress>" + esc(info.mac) + "</tt:HwAddress><tt:MTU>1500</tt:MTU></tt:Info><tt:IPv4>"
                        + "<tt:Enabled>true</tt:Enabled><tt:Config><tt:FromDHCP><tt:Address>" + esc(info.host) + "</tt:Address><tt:PrefixLength>24"
                        + "</tt:PrefixLength></tt:FromDHCP><tt:DHCP>true</tt:DHCP></tt:Config></tt:IPv4></tds:NetworkInterfaces>"
                        + "</tds:GetNetworkInterfacesResponse>";
            case "GetHostname":
                return "<tds:GetHostnameResponse><tds:HostnameInformation><tt:FromDHCP>false</tt:FromDHCP><tt:Name>door-sign</tt:Name>"
                        + "</tds:HostnameInformation></tds:GetHostnameResponse>";
            case "GetScopes": {
                StringBuilder scopes = new StringBuilder("<tds:GetScopesResponse>");
                for (String scope : Discovery.scopes(name(info), info.model)) {
                    scopes.append("<tds:Scopes><tt:ScopeDef>Fixed</tt:ScopeDef><tt:ScopeItem>").append(esc(scope)).append("</tt:ScopeItem></tds:Scopes>");
                }
                return scopes.append("</tds:GetScopesResponse>").toString();
            }
            case "GetProfiles":
                return "<trt:GetProfilesResponse>" + profile("trt:Profiles", info.login.sound) + "</trt:GetProfilesResponse>";
            case "GetProfile":
                return "<trt:GetProfileResponse>" + profile("trt:Profile", info.login.sound) + "</trt:GetProfileResponse>";
            case "GetVideoSources":
                return "<trt:GetVideoSourcesResponse><trt:VideoSources token=\"vs\"><tt:Framerate>" + FPS + "</tt:Framerate><tt:Resolution><tt:Width>"
                        + WIDTH + "</tt:Width><tt:Height>" + HEIGHT + "</tt:Height></tt:Resolution></trt:VideoSources></trt:GetVideoSourcesResponse>";
            case "GetVideoSourceConfigurations":
                return "<trt:GetVideoSourceConfigurationsResponse>" + sourceConfiguration("trt:Configurations")
                        + "</trt:GetVideoSourceConfigurationsResponse>";
            case "GetVideoEncoderConfigurations":
                return "<trt:GetVideoEncoderConfigurationsResponse>" + encoderConfiguration("trt:Configurations")
                        + "</trt:GetVideoEncoderConfigurationsResponse>";
            case "GetAudioSources":
                if (!info.login.sound) return null;
                return "<trt:GetAudioSourcesResponse><trt:AudioSources token=\"as\"><tt:Channels>1</tt:Channels></trt:AudioSources>"
                        + "</trt:GetAudioSourcesResponse>";
            case "GetAudioSourceConfigurations":
                if (!info.login.sound) return null;
                return "<trt:GetAudioSourceConfigurationsResponse>" + soundSourceConfiguration("trt:Configurations")
                        + "</trt:GetAudioSourceConfigurationsResponse>";
            case "GetAudioEncoderConfigurations":
                if (!info.login.sound) return null;
                return "<trt:GetAudioEncoderConfigurationsResponse>" + soundEncoderConfiguration("trt:Configurations")
                        + "</trt:GetAudioEncoderConfigurationsResponse>";
            case "GetStreamUri":
                return "<trt:GetStreamUriResponse><trt:MediaUri><tt:Uri>rtsp://" + esc(info.host) + ":" + RTSP_PORT + streamPath(info.login.password)
                        + "</tt:Uri><tt:InvalidAfterConnect>false</tt:InvalidAfterConnect><tt:InvalidAfterReboot>false</tt:InvalidAfterReboot>"
                        + "<tt:Timeout>PT0S</tt:Timeout></trt:MediaUri></trt:GetStreamUriResponse>";
            default:
                return null;
        }
    }

    private static String name(Info info) {
        return info.login.name.isEmpty() ? "Door sign" : info.login.name;
    }

    private static String service(String namespace, String xaddr) {
        return "<tds:Service><tds:Namespace>" + namespace + "</tds:Namespace><tds:XAddr>" + xaddr + "</tds:XAddr><tds:Version><tt:Major>2</tt:Major>"
                + "<tt:Minor>5</tt:Minor></tds:Version></tds:Service>";
    }

    /** The one profile; with sound, its microphone too, in the schema's order (video source, audio source, video encoder, audio encoder). */
    private static String profile(String element, boolean sound) {
        return "<" + element + " token=\"main\" fixed=\"true\"><tt:Name>main</tt:Name>" + sourceConfiguration("tt:VideoSourceConfiguration")
                + (sound ? soundSourceConfiguration("tt:AudioSourceConfiguration") : "") + encoderConfiguration("tt:VideoEncoderConfiguration")
                + (sound ? soundEncoderConfiguration("tt:AudioEncoderConfiguration") : "") + "</" + element + ">";
    }

    private static String soundSourceConfiguration(String element) {
        return "<" + element + " token=\"asc\"><tt:Name>asc</tt:Name><tt:UseCount>1</tt:UseCount><tt:SourceToken>as</tt:SourceToken></" + element + ">";
    }

    private static String soundEncoderConfiguration(String element) {
        return "<" + element + " token=\"aec\"><tt:Name>aec</tt:Name><tt:UseCount>1</tt:UseCount><tt:Encoding>AAC</tt:Encoding><tt:Bitrate>"
                + SOUND_BITRATE_KBPS + "</tt:Bitrate><tt:SampleRate>" + SAMPLE_RATE / 1000 + "</tt:SampleRate>" + MULTICAST
                + "<tt:SessionTimeout>PT60S</tt:SessionTimeout></" + element + ">";
    }

    // go2rtc's ONVIF server for Protect (PR AlexxIT/go2rtc#1520): Protect breaks without the source configuration, and needs RateControl
    private static String sourceConfiguration(String element) {
        return "<" + element + " token=\"vsc\"><tt:Name>vsc</tt:Name><tt:UseCount>1</tt:UseCount><tt:SourceToken>vs</tt:SourceToken>"
                + "<tt:Bounds x=\"0\" y=\"0\" width=\"" + WIDTH + "\" height=\"" + HEIGHT + "\"/></" + element + ">";
    }

    private static String encoderConfiguration(String element) {
        return "<" + element + " token=\"vec\"><tt:Name>vec</tt:Name><tt:UseCount>1</tt:UseCount><tt:Encoding>H264</tt:Encoding><tt:Resolution>"
                + "<tt:Width>" + WIDTH + "</tt:Width><tt:Height>" + HEIGHT + "</tt:Height></tt:Resolution><tt:Quality>5</tt:Quality><tt:RateControl>"
                + "<tt:FrameRateLimit>" + FPS + "</tt:FrameRateLimit><tt:EncodingInterval>1</tt:EncodingInterval><tt:BitrateLimit>" + BITRATE_KBPS
                + "</tt:BitrateLimit></tt:RateControl><tt:H264><tt:GovLength>" + GOP + "</tt:GovLength><tt:H264Profile>Main</tt:H264Profile></tt:H264>"
                + MULTICAST + "<tt:SessionTimeout>PT60S</tt:SessionTimeout></" + element + ">";
    }

    private static final String MULTICAST = "<tt:Multicast><tt:Address><tt:Type>IPv4</tt:Type><tt:IPv4Address>0.0.0.0</tt:IPv4Address></tt:Address>"
            + "<tt:Port>0</tt:Port><tt:TTL>1</tt:TTL><tt:AutoStart>false</tt:AutoStart></tt:Multicast>";

    private static String envelope(String body) {
        return "<?xml version=\"1.0\" encoding=\"UTF-8\"?><s:Envelope xmlns:s=\"http://www.w3.org/2003/05/soap-envelope\" "
                + "xmlns:tds=\"http://www.onvif.org/ver10/device/wsdl\" xmlns:trt=\"http://www.onvif.org/ver10/media/wsdl\" "
                + "xmlns:tt=\"http://www.onvif.org/ver10/schema\" xmlns:ter=\"http://www.onvif.org/ver10/error\"><s:Body>" + body + "</s:Body></s:Envelope>";
    }

    private static String fault(String code, String subcode, String reason) {
        return envelope("<s:Fault><s:Code><s:Value>" + code + "</s:Value><s:Subcode><s:Value>" + subcode + "</s:Value></s:Subcode></s:Code>"
                + "<s:Reason><s:Text xml:lang=\"en\">" + reason + "</s:Text></s:Reason></s:Fault>");
    }

    private static Pattern element(String name) {
        return Pattern.compile("<(?:[\\w.-]+:)?" + name + "\\b[^>]*>([^<]*)<", Pattern.DOTALL);
    }

    private static String first(Pattern pattern, String text) {
        Matcher m = pattern.matcher(text);
        return m.find() ? m.group(1) : null;
    }

    private static boolean constantEquals(String a, String b) {
        return MessageDigest.isEqual(a.getBytes(StandardCharsets.UTF_8), b.getBytes(StandardCharsets.UTF_8));
    }

    static String esc(String text) {
        return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("\"", "&quot;").replace("'", "&apos;");
    }

    static String unescape(String text) {
        return text.replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", "\"").replace("&apos;", "'").replace("&amp;", "&");
    }
}
