package to.croom.doorsign;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertNull;
import static org.junit.Assert.assertTrue;

import java.time.Duration;
import java.time.Instant;
import java.util.regex.Matcher;
import java.util.regex.Pattern;
import org.junit.Test;

public class OnvifEventsTest {

    static final Instant NOW = OnvifTest.NOW;
    static final String SERVICE = "/onvif/event_service";
    static final String TEV = " xmlns:tev=\"http://www.onvif.org/ver10/events/wsdl\" xmlns:wsnt=\"http://docs.oasis-open.org/wsn/b-2\"";

    /** A request with something inside its action, logged in as Protect is unless `user` is null. */
    static String request(String action, String inside, String user) throws Exception {
        return OnvifTest.request("X", user, "secret-pw1", NOW).replace("<X/>", "<" + action + TEV + ">" + inside + "</" + action + ">");
    }

    static Onvif.Reply ask(Events events, String path, String action, String inside) throws Exception {
        return Onvif.handle(path, request(action, inside, "camera"), OnvifTest.INFO, events, NOW);
    }

    /** A subscription made as Protect makes one: its path on the camera. */
    static String subscribe(Events events) throws Exception {
        Onvif.Reply reply = ask(events, SERVICE, "tev:CreatePullPointSubscription", "<tev:InitialTerminationTime>PT60S</tev:InitialTerminationTime>");
        assertEquals(200, reply.status);
        Matcher address = Pattern.compile("<wsa:Address>http://192\\.168\\.0\\.194:8000(/onvif/subscription/[0-9a-f]+)</wsa:Address>").matcher(reply.xml);
        assertTrue(reply.xml, address.find());
        return address.group(1);
    }

    static Onvif.Reply pull(Events events, String path) throws Exception {
        return ask(events, path, "tev:PullMessages", "<tev:Timeout>PT0S</tev:Timeout><tev:MessageLimit>10</tev:MessageLimit>");
    }

    @Test public void withPeopleDetectionTheCameraNamesItsEventService() throws Exception {
        Events events = new Events();
        for (String action : new String[] {"tds:GetServices", "tds:GetCapabilities"}) {
            Onvif.Reply reply = ask(events, "/onvif/device_service", action, "");
            assertTrue(action, reply.xml.contains("http://192.168.0.194:8000/onvif/event_service"));
            assertTrue(action, reply.xml.contains("http://192.168.0.194:8000/onvif/media_service"));
            OnvifTest.wellFormed(reply.xml);
        }
        assertTrue(ask(events, "/onvif/device_service", "tds:GetServices", "").xml.contains("http://www.onvif.org/ver10/events/wsdl"));
        String capabilities = ask(events, "/onvif/device_service", "tds:GetCapabilities", "").xml;
        assertTrue(capabilities.contains("<tt:WSPullPointSupport>true</tt:WSPullPointSupport>"));
        assertTrue(capabilities.indexOf("<tt:Device>") < capabilities.indexOf("<tt:Events>")
                && capabilities.indexOf("<tt:Events>") < capabilities.indexOf("<tt:Media>"));       // the schema's order
    }

    @Test public void withoutPeopleDetectionThereIsNoEventService() throws Exception {
        for (String action : new String[] {"tds:GetServices", "tds:GetCapabilities"}) {
            assertFalse(action, OnvifTest.ask(action).xml.contains("vent"));
        }
        for (String action : new String[] {"tev:CreatePullPointSubscription", "tev:GetEventProperties", "tev:PullMessages"}) {
            Onvif.Reply reply = ask(null, SERVICE, action, "");
            assertEquals(action, 400, reply.status);
            assertTrue(action, reply.xml.contains("ter:ActionNotSupported"));
        }
    }

    @Test public void theEventsNeedTheLogin() throws Exception {
        Events events = new Events();
        String path = subscribe(events);
        for (String[] ask : new String[][] {{SERVICE, "tev:CreatePullPointSubscription"}, {SERVICE, "tev:GetEventProperties"},
                {path, "tev:PullMessages"}, {path, "wsnt:Renew"}, {path, "wsnt:Unsubscribe"}}) {
            Onvif.Reply reply = Onvif.handle(ask[0], request(ask[1], "", null), OnvifTest.INFO, events, NOW);
            assertEquals(ask[1], 401, reply.status);
        }
        assertEquals(1, events.size());                 // nothing was made, and nothing ended, without it
    }

    @Test public void theTopicsAreTheTwoARecorderReadsMotionFrom() throws Exception {
        Onvif.Reply reply = ask(new Events(), SERVICE, "tev:GetEventProperties", "");
        assertEquals(200, reply.status);
        for (String needle : new String[] {"<tns1:RuleEngine><CellMotionDetector><Motion wstop:topic=\"true\">",
                "<tns1:VideoSource><MotionAlarm wstop:topic=\"true\">", "Name=\"IsMotion\" Type=\"xs:boolean\"", "Name=\"State\" Type=\"xs:boolean\"",
                "IsProperty=\"true\"", "<wsnt:FixedTopicSet>true</wsnt:FixedTopicSet>",
                "<wsa:Action>http://www.onvif.org/ver10/events/wsdl/EventPortType/GetEventPropertiesResponse</wsa:Action>"}) {
            assertTrue(needle, reply.xml.contains(needle));
        }
        OnvifTest.wellFormed(reply.xml);
    }

    @Test public void aSubscriptionIsToldHowThingsStandAndThenTheNews() throws Exception {
        Events events = new Events();
        String path = subscribe(events);
        Onvif.Reply first = pull(events, path);
        assertEquals(200, first.status);
        assertEquals(2, first.xml.split("<wsnt:NotificationMessage>", -1).length - 1);
        for (String needle : new String[] {"<tev:CurrentTime>2026-11-06T18:00:00Z</tev:CurrentTime>", "<tev:TerminationTime>",
                "<wsnt:Topic Dialect=\"http://www.onvif.org/ver10/tev/topicExpression/ConcreteSet\">tns1:RuleEngine/CellMotionDetector/Motion<",
                "PropertyOperation=\"Initialized\"", "<tt:SimpleItem Name=\"IsMotion\" Value=\"false\"/>",
                "<tt:SimpleItem Name=\"VideoSourceConfigurationToken\" Value=\"vsc\"/>", "<tt:SimpleItem Name=\"Source\" Value=\"vs\"/>",
                "<wsa:Action>http://www.onvif.org/ver10/events/wsdl/PullPointSubscription/PullMessagesResponse</wsa:Action>"}) {
            assertTrue(needle, first.xml.contains(needle));
        }
        OnvifTest.wellFormed(first.xml);
        assertFalse(pull(events, path).xml.contains("NotificationMessage"));

        events.motion(true, NOW.plusSeconds(3));
        Onvif.Reply came = pull(events, path);
        assertTrue(came.xml.contains("PropertyOperation=\"Changed\"") && came.xml.contains("<tt:SimpleItem Name=\"IsMotion\" Value=\"true\"/>"));
        assertTrue(came.xml.contains("<tt:SimpleItem Name=\"State\" Value=\"true\"/>"));
        OnvifTest.wellFormed(came.xml);
    }

    @Test public void theMessageLimitIsKept() throws Exception {
        Events events = new Events();
        String path = subscribe(events);
        Onvif.Reply one = ask(events, path, "tev:PullMessages", "<tev:Timeout>PT0S</tev:Timeout><tev:MessageLimit>1</tev:MessageLimit>");
        assertEquals(1, one.xml.split("<wsnt:NotificationMessage>", -1).length - 1);
    }

    @Test public void renewUnsubscribeAndSynchronize() throws Exception {
        Events events = new Events();
        String path = subscribe(events);
        pull(events, path);
        Onvif.Reply renewed = ask(events, path, "wsnt:Renew", "<wsnt:TerminationTime>PT10M</wsnt:TerminationTime>");
        assertEquals(200, renewed.status);
        assertTrue(renewed.xml.contains("<wsnt:TerminationTime>2026-11-06T18:10:00Z</wsnt:TerminationTime>"));
        assertTrue(renewed.xml.contains("<wsa:Action>http://docs.oasis-open.org/wsn/bw-2/SubscriptionManager/RenewResponse</wsa:Action>"));
        OnvifTest.wellFormed(renewed.xml);
        Onvif.Reply absolute = ask(events, path, "wsnt:Renew", "<wsnt:TerminationTime>2026-11-06T18:05:00Z</wsnt:TerminationTime>");
        assertTrue(absolute.xml.contains("<wsnt:TerminationTime>2026-11-06T18:05:00Z</wsnt:TerminationTime>"));

        assertEquals(200, ask(events, path, "tev:SetSynchronizationPoint", "").status);
        assertTrue(pull(events, path).xml.contains("PropertyOperation=\"Initialized\""));

        Onvif.Reply ended = ask(events, path, "wsnt:Unsubscribe", "");
        assertEquals(200, ended.status);
        OnvifTest.wellFormed(ended.xml);
        assertEquals(0, events.size());
    }

    @Test public void aSubscriptionThatIsGoneSaysSoRatherThanNotSupported() throws Exception {
        Events events = new Events();
        for (String path : new String[] {"/onvif/subscription/1234abcd", SERVICE, "/onvif/subscription/../x"}) {
            for (String action : new String[] {"tev:PullMessages", "wsnt:Renew", "wsnt:Unsubscribe", "tev:SetSynchronizationPoint"}) {
                Onvif.Reply reply = ask(events, path, action, "");
                assertEquals(action, 400, reply.status);
                assertTrue(action, reply.xml.contains("No such subscription") && !reply.xml.contains("ActionNotSupported"));
                OnvifTest.wellFormed(reply.xml);
            }
        }
    }

    @Test public void theEventServicesCapabilitiesAreAnsweredOnItsPathOnly() throws Exception {
        Events events = new Events();
        Onvif.Reply reply = ask(events, SERVICE, "tev:GetServiceCapabilities", "");
        assertEquals(200, reply.status);
        assertTrue(reply.xml.contains("WSPullPointSupport=\"true\"") && reply.xml.contains("MaxPullPoints=\"8\""));
        OnvifTest.wellFormed(reply.xml);
        assertEquals(400, ask(events, "/onvif/device_service", "tds:GetServiceCapabilities", "").status);
    }

    @Test public void timesAreReadAsOnvifWritesThem() {
        assertEquals(Duration.ofSeconds(60), OnvifEvents.time("PT60S", NOW));
        assertEquals(Duration.ofMinutes(1), OnvifEvents.time(" PT1M ", NOW));
        assertEquals(Duration.ofSeconds(90), OnvifEvents.time("2026-11-06T18:01:30Z", NOW));
        assertEquals(Duration.ofSeconds(90), OnvifEvents.time("2026-11-06T19:01:30+01:00", NOW));
        assertNull(OnvifEvents.time("soon", NOW));
        assertNull(OnvifEvents.time("", NOW));
        assertNull(OnvifEvents.time(null, NOW));
    }
}
