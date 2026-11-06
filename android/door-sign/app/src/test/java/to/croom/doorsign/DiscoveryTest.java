package to.croom.doorsign;

import static org.junit.Assert.assertNull;
import static org.junit.Assert.assertTrue;

import org.junit.Test;

public class DiscoveryTest {

    static final String URN = "urn:uuid:2a0c3c1e-1111-3222-8333-444455556666";
    static final String XADDR = "http://192.168.0.194:8000/onvif/device_service";

    static String probe(String types) {
        return "<?xml version=\"1.0\" encoding=\"UTF-8\"?><s:Envelope xmlns:s=\"http://www.w3.org/2003/05/soap-envelope\" "
                + "xmlns:a=\"http://schemas.xmlsoap.org/ws/2004/08/addressing\" xmlns:d=\"http://schemas.xmlsoap.org/ws/2005/04/discovery\" "
                + "xmlns:dn=\"http://www.onvif.org/ver10/network/wsdl\"><s:Header>"
                + "<a:MessageID>uuid:7d8f1d2e-0000-4000-8000-000000000001</a:MessageID>"
                + "<a:To>urn:schemas-xmlsoap-org:ws:2005:04:discovery</a:To>"
                + "<a:Action>http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe</a:Action></s:Header>"
                + "<s:Body><d:Probe>" + types + "</d:Probe></s:Body></s:Envelope>";
    }

    @Test public void probeIsAnsweredWithTheRelatesTo() {
        String match = Discovery.probeMatch(probe("<d:Types>dn:NetworkVideoTransmitter</d:Types>"), URN, XADDR, "Room <B> door sign", "WF2489T");
        assertTrue(match.contains("<a:RelatesTo>uuid:7d8f1d2e-0000-4000-8000-000000000001</a:RelatesTo>"));
        assertTrue(match.contains("<d:XAddrs>" + XADDR + "</d:XAddrs>"));
        assertTrue(match.contains("<a:Address>" + URN + "</a:Address>"));
        assertTrue(match.contains("onvif://www.onvif.org/name/Room%20%3CB%3E%20door%20sign"));
        assertTrue(match.contains("onvif://www.onvif.org/hardware/WF2489T"));
        assertTrue(match.contains("ProbeMatches"));
    }

    @Test public void aProbeForAnyDeviceOrForAnOnvifDeviceIsAnswered() {
        assertTrue(Discovery.probeMatch(probe(""), URN, XADDR, "n", "m") != null);
        assertTrue(Discovery.probeMatch(probe("<d:Types>tds:Device</d:Types>"), URN, XADDR, "n", "m") != null);
        assertTrue(Discovery.probeMatch(probe("<d:Types xmlns:tds=\"x\">tds:Device dn:NetworkVideoTransmitter</d:Types>"), URN, XADDR, "n", "m") != null);
    }

    @Test public void otherTypesAreIgnored() {
        assertNull(Discovery.probeMatch(probe("<d:Types>wsdp:Printer</d:Types>"), URN, XADDR, "n", "m"));
        assertNull(Discovery.probeMatch(probe("").replace("d:Probe>", "d:ProbeMatches>"), URN, XADDR, "n", "m"));
        assertNull(Discovery.probeMatch("not xml at all", URN, XADDR, "n", "m"));
    }
}
