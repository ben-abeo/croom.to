package to.croom.doorsign;

import java.time.Duration;
import java.time.Instant;
import java.time.OffsetDateTime;
import java.time.temporal.ChronoUnit;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * The camera's ONVIF event service (spec 2026-11-22): what UniFi Protect asks to hear of motion from a camera that is not Ubiquiti's.
 * It reads the camera's services when it adopts it, makes a PullPoint subscription, and asks that for news again and again. The
 * service is at /onvif/event_service and each subscription at /onvif/subscription/<id>: Protect sends no WS-Addressing headers, so a
 * request is told apart by its path. Onvif has checked the login before any of this.
 */
final class OnvifEvents {

    static final String NAMESPACE = "http://www.onvif.org/ver10/events/wsdl";
    static final String SERVICE_PATH = "/onvif/event_service";
    static final String SUBSCRIPTION_PATH = "/onvif/subscription/";

    private static final String PORT_ACTIONS = NAMESPACE + "/EventPortType/";
    private static final String PULL_ACTIONS = NAMESPACE + "/PullPointSubscription/";
    private static final String MANAGER_ACTIONS = "http://docs.oasis-open.org/wsn/bw-2/SubscriptionManager/";
    private static final Pattern SUBSCRIPTION = Pattern.compile(Pattern.quote(SUBSCRIPTION_PATH) + "([0-9a-f]{1,16})");
    private static final Pattern INITIAL_TERMINATION = Onvif.element("InitialTerminationTime");
    private static final Pattern TERMINATION = Onvif.element("TerminationTime");
    private static final Pattern TIMEOUT = Onvif.element("Timeout");
    private static final Pattern LIMIT = Onvif.element("MessageLimit");

    private OnvifEvents() {}

    /** The answer to an event request, or null when `action` is not one of the event service's. `base` is "http://host:port". */
    static Onvif.Reply handle(String action, String path, String soap, String base, Events events, Instant now) {
        switch (action) {
            case "GetServiceCapabilities":
                if (!path.equals(SERVICE_PATH)) return null;      // the device and media services have one too, which the camera does not answer
                return reply(action, PORT_ACTIONS, "<tev:GetServiceCapabilitiesResponse><tev:Capabilities WSSubscriptionPolicySupport=\"false\" "
                        + "WSPullPointSupport=\"true\" WSPausableSubscriptionManagerInterfaceSupport=\"false\" MaxNotificationProducers=\"0\" "
                        + "MaxPullPoints=\"" + Events.MAX_SUBSCRIPTIONS + "\" PersistentNotificationStorage=\"false\"/>"
                        + "</tev:GetServiceCapabilitiesResponse>");
            case "GetEventProperties":
                return reply(action, PORT_ACTIONS, PROPERTIES);
            case "CreatePullPointSubscription": {
                String id = events.subscribe(time(Onvif.first(INITIAL_TERMINATION, soap), now), now);
                return reply(action, PORT_ACTIONS, "<tev:CreatePullPointSubscriptionResponse><tev:SubscriptionReference><wsa:Address>" + base
                        + SUBSCRIPTION_PATH + id + "</wsa:Address></tev:SubscriptionReference><wsnt:CurrentTime>" + stamp(now)
                        + "</wsnt:CurrentTime><wsnt:TerminationTime>" + stamp(events.expires(id, now)) + "</wsnt:TerminationTime>"
                        + "</tev:CreatePullPointSubscriptionResponse>");
            }
            case "PullMessages": {
                Events.Pulled pulled = events.pull(subscription(path), time(Onvif.first(TIMEOUT, soap), now), limit(soap), now);
                if (pulled == null) return gone(action);
                StringBuilder body = new StringBuilder("<tev:PullMessagesResponse><tev:CurrentTime>").append(stamp(pulled.at))
                        .append("</tev:CurrentTime><tev:TerminationTime>").append(stamp(pulled.expires)).append("</tev:TerminationTime>");
                for (String message : pulled.messages) body.append(message);
                return reply(action, PULL_ACTIONS, body.append("</tev:PullMessagesResponse>").toString());
            }
            case "SetSynchronizationPoint":
                if (!events.synchronize(subscription(path), now)) return gone(action);
                return reply(action, PULL_ACTIONS, "<tev:SetSynchronizationPointResponse/>");
            case "Renew": {
                Instant until = events.renew(subscription(path), time(Onvif.first(TERMINATION, soap), now), now);
                if (until == null) return gone(action);
                return reply(action, MANAGER_ACTIONS, "<wsnt:RenewResponse><wsnt:TerminationTime>" + stamp(until) + "</wsnt:TerminationTime>"
                        + "<wsnt:CurrentTime>" + stamp(now) + "</wsnt:CurrentTime></wsnt:RenewResponse>");
            }
            case "Unsubscribe":
                if (!events.unsubscribe(subscription(path))) return gone(action);
                return reply(action, MANAGER_ACTIONS, "<wsnt:UnsubscribeResponse/>");
            default:
                return null;
        }
    }

    /** The subscription a request's path names, or null. */
    static String subscription(String path) {
        Matcher m = SUBSCRIPTION.matcher(path);
        return m.matches() ? m.group(1) : null;
    }

    /** A time as ONVIF gives one, "PT60S" from now or "2026-11-22T18:01:00Z", as how long from now; null when it is neither. */
    static Duration time(String text, Instant now) {
        if (text == null || text.trim().isEmpty()) return null;
        try {
            return Duration.parse(text.trim());
        } catch (RuntimeException notADuration) {
            try {
                return Duration.between(now, OffsetDateTime.parse(text.trim()).toInstant());
            } catch (RuntimeException e) {
                return null;
            }
        }
    }

    private static int limit(String soap) {
        try {
            return Math.max(1, Math.min(Integer.parseInt(Onvif.first(LIMIT, soap).trim()), Events.MAX_QUEUED));
        } catch (RuntimeException e) {
            return Events.MAX_QUEUED;
        }
    }

    private static String stamp(Instant when) {
        return when.truncatedTo(ChronoUnit.SECONDS).toString();
    }

    private static Onvif.Reply gone(String action) {
        return new Onvif.Reply(400, Onvif.fault("s:Sender", "ter:InvalidArgVal", "No such subscription"), action);
    }

    private static Onvif.Reply reply(String action, String actions, String body) {
        return new Onvif.Reply(200, "<?xml version=\"1.0\" encoding=\"UTF-8\"?><s:Envelope xmlns:s=\"http://www.w3.org/2003/05/soap-envelope\" "
                + "xmlns:tev=\"" + NAMESPACE + "\" xmlns:wsnt=\"http://docs.oasis-open.org/wsn/b-2\" "
                + "xmlns:wstop=\"http://docs.oasis-open.org/wsn/t-1\" xmlns:wsa=\"http://www.w3.org/2005/08/addressing\" "
                + "xmlns:tns1=\"http://www.onvif.org/ver10/topics\" xmlns:tt=\"http://www.onvif.org/ver10/schema\" "
                + "xmlns:xs=\"http://www.w3.org/2001/XMLSchema\"><s:Header><wsa:Action>" + actions + action + "Response</wsa:Action></s:Header>"
                + "<s:Body>" + body + "</s:Body></s:Envelope>", action);
    }

    // The two topics a recorder reads motion from, each a property (it has a state, on or off)
    private static final String PROPERTIES = "<tev:GetEventPropertiesResponse>"
            + "<tev:TopicNamespaceLocation>http://www.onvif.org/onvif/ver10/topics/topicns.xml</tev:TopicNamespaceLocation>"
            + "<wsnt:FixedTopicSet>true</wsnt:FixedTopicSet><wstop:TopicSet><tns1:RuleEngine><CellMotionDetector><Motion wstop:topic=\"true\">"
            + "<tt:MessageDescription IsProperty=\"true\"><tt:Source>"
            + "<tt:SimpleItemDescription Name=\"VideoSourceConfigurationToken\" Type=\"tt:ReferenceToken\"/>"
            + "<tt:SimpleItemDescription Name=\"VideoAnalyticsConfigurationToken\" Type=\"tt:ReferenceToken\"/>"
            + "<tt:SimpleItemDescription Name=\"Rule\" Type=\"xs:string\"/></tt:Source><tt:Data>"
            + "<tt:SimpleItemDescription Name=\"IsMotion\" Type=\"xs:boolean\"/></tt:Data></tt:MessageDescription></Motion></CellMotionDetector>"
            + "</tns1:RuleEngine><tns1:VideoSource><MotionAlarm wstop:topic=\"true\"><tt:MessageDescription IsProperty=\"true\"><tt:Source>"
            + "<tt:SimpleItemDescription Name=\"Source\" Type=\"tt:ReferenceToken\"/></tt:Source><tt:Data>"
            + "<tt:SimpleItemDescription Name=\"State\" Type=\"xs:boolean\"/></tt:Data></tt:MessageDescription></MotionAlarm></tns1:VideoSource>"
            + "</wstop:TopicSet><wsnt:TopicExpressionDialect>" + Events.DIALECT + "</wsnt:TopicExpressionDialect>"
            + "<wsnt:TopicExpressionDialect>http://docs.oasis-open.org/wsn/t-1/TopicExpression/Concrete</wsnt:TopicExpressionDialect>"
            + "<tev:MessageContentFilterDialect>http://www.onvif.org/ver10/tev/messageContentFilter/ItemFilter</tev:MessageContentFilterDialect>"
            + "<tev:MessageContentSchemaLocation>http://www.onvif.org/onvif/ver10/schema/onvif.xsd</tev:MessageContentSchemaLocation>"
            + "</tev:GetEventPropertiesResponse>";
}
