package to.croom.doorsign;

import java.net.URLEncoder;
import java.nio.charset.StandardCharsets;
import java.util.Arrays;
import java.util.List;
import java.util.UUID;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * WS-Discovery for the camera (spec 2026-11-06): UniFi Protect finds third-party cameras by multicasting a Probe to 239.255.255.250:3702;
 * the panel answers one that asks for an ONVIF camera (or for anything) with a ProbeMatch naming its ONVIF address.
 */
final class Discovery {

    static final int PORT = 3702;
    static final String GROUP = "239.255.255.250";

    private static final Pattern BODY_ACTION = Pattern.compile("<(?:[\\w.-]+:)?Body\\b[^>]*>\\s*<(?:[\\w.-]+:)?([\\w.-]+)");
    private static final Pattern MESSAGE_ID = Pattern.compile("<(?:[\\w.-]+:)?MessageID\\b[^>]*>([^<]*)<");
    private static final Pattern TYPES = Pattern.compile("<(?:[\\w.-]+:)?Types\\b[^>]*>([^<]*)<");
    private static final List<String> OURS = Arrays.asList("NetworkVideoTransmitter", "Device");

    private Discovery() {}

    /** The ProbeMatches to send back, or null when the message is not a Probe or asks for something else than an ONVIF camera. */
    static String probeMatch(String probeXml, String endpointUrn, String xaddr, String name, String model) {
        Matcher action = BODY_ACTION.matcher(probeXml);
        if (!action.find() || !action.group(1).equals("Probe")) return null;
        Matcher types = TYPES.matcher(probeXml);
        if (types.find()) {
            for (String type : types.group(1).trim().split("\\s+")) {
                if (type.isEmpty()) continue;
                String local = type.substring(type.indexOf(':') + 1);
                if (!OURS.contains(local)) return null;
            }
        }
        Matcher id = MESSAGE_ID.matcher(probeXml);
        String relatesTo = id.find() ? id.group(1).trim() : "";
        return "<?xml version=\"1.0\" encoding=\"UTF-8\"?><s:Envelope xmlns:s=\"http://www.w3.org/2003/05/soap-envelope\" "
                + "xmlns:a=\"http://schemas.xmlsoap.org/ws/2004/08/addressing\" xmlns:d=\"http://schemas.xmlsoap.org/ws/2005/04/discovery\" "
                + "xmlns:dn=\"http://www.onvif.org/ver10/network/wsdl\" xmlns:tds=\"http://www.onvif.org/ver10/device/wsdl\"><s:Header>"
                + "<a:MessageID>urn:uuid:" + UUID.randomUUID() + "</a:MessageID><a:RelatesTo>" + Onvif.esc(relatesTo) + "</a:RelatesTo>"
                + "<a:To>http://schemas.xmlsoap.org/ws/2004/08/addressing/role/anonymous</a:To>"
                + "<a:Action>http://schemas.xmlsoap.org/ws/2005/04/discovery/ProbeMatches</a:Action></s:Header><s:Body><d:ProbeMatches><d:ProbeMatch>"
                + "<a:EndpointReference><a:Address>" + Onvif.esc(endpointUrn) + "</a:Address></a:EndpointReference>"
                + "<d:Types>dn:NetworkVideoTransmitter tds:Device</d:Types><d:Scopes>" + Onvif.esc(String.join(" ", scopes(name, model)))
                + "</d:Scopes><d:XAddrs>" + Onvif.esc(xaddr) + "</d:XAddrs><d:MetadataVersion>1</d:MetadataVersion></d:ProbeMatch></d:ProbeMatches>"
                + "</s:Body></s:Envelope>";
    }

    /** The camera's ONVIF scopes: what it is, its name and its hardware, each a URI. */
    static List<String> scopes(String name, String model) {
        return Arrays.asList("onvif://www.onvif.org/type/video_encoder", "onvif://www.onvif.org/Profile/Streaming",
                "onvif://www.onvif.org/name/" + uri(name), "onvif://www.onvif.org/hardware/" + uri(model));
    }

    private static String uri(String text) {
        try {
            return URLEncoder.encode(text, "UTF-8").replace("+", "%20");
        } catch (java.io.UnsupportedEncodingException e) {
            throw new IllegalStateException(e);
        }
    }
}
