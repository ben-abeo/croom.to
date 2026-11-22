package to.croom.doorsign;

import java.security.SecureRandom;
import java.time.Duration;
import java.time.Instant;
import java.time.temporal.ChronoUnit;
import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.Iterator;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * What the camera tells UniFi Protect about people (spec 2026-11-22): one yes or no, "someone is in the picture", kept as ONVIF's two
 * motion topics, the only detections Protect takes from a camera that is not Ubiquiti's. A recorder subscribes (a PullPoint), is told
 * how things stand at once, and then asks again and again; each asking waits here until the answer changes or its time is up.
 * OnvifEvents turns the SOAP requests into these calls.
 */
final class Events {

    static final int MAX_SUBSCRIPTIONS = 8;
    static final int MAX_QUEUED = 100;                          // per subscription: behind that, the oldest news is dropped
    static final int MAX_WAITING = 4;                           // askings held open at once: the rest are answered at once, so ONVIF stays reachable
    static final Duration MAX_WAIT = Duration.ofSeconds(60);
    static final Duration MIN_LIFE = Duration.ofSeconds(10);
    static final Duration MAX_LIFE = Duration.ofHours(1);
    static final Duration DEFAULT_LIFE = Duration.ofSeconds(60);
    static final Duration KEEP = Duration.ofSeconds(60);        // a subscription that is asked lives at least this long after the asking

    static final String MOTION_TOPIC = "tns1:RuleEngine/CellMotionDetector/Motion";
    static final String ALARM_TOPIC = "tns1:VideoSource/MotionAlarm";
    static final String DIALECT = "http://www.onvif.org/ver10/tev/topicExpression/ConcreteSet";

    private static final class Subscription {
        final ArrayDeque<String> queue = new ArrayDeque<>();
        Instant expires;
    }

    /** What one asking got: the news (NotificationMessage elements), when it was answered and until when the subscription lives. */
    static final class Pulled {
        final List<String> messages;
        final Instant at;
        final Instant expires;

        Pulled(List<String> messages, Instant at, Instant expires) {
            this.messages = messages;
            this.at = at;
            this.expires = expires;
        }
    }

    private final Map<String, Subscription> subscriptions = new LinkedHashMap<>();
    private final SecureRandom random = new SecureRandom();
    private boolean motion;
    private boolean closed;
    private int waiting;

    /** Someone came into the picture, or the last one left it: every subscriber is told, and a waiting asking answers now. */
    synchronized void motion(boolean on, Instant now) {
        if (on == motion) return;
        motion = on;
        for (Subscription s : subscriptions.values()) queue(s, "Changed", now);
        notifyAll();
    }

    synchronized boolean motion() {
        return motion;
    }

    /** A new subscription's id; it starts with how things stand now. When all are taken, the one nearest its end makes room. */
    synchronized String subscribe(Duration life, Instant now) {
        purge(now);
        if (subscriptions.size() >= MAX_SUBSCRIPTIONS) {
            String oldest = null;
            for (Map.Entry<String, Subscription> e : subscriptions.entrySet()) {
                if (oldest == null || e.getValue().expires.isBefore(subscriptions.get(oldest).expires)) oldest = e.getKey();
            }
            subscriptions.remove(oldest);
            notifyAll();
        }
        Subscription s = new Subscription();
        s.expires = now.plus(life(life));
        queue(s, "Initialized", now);
        String id = Long.toHexString(random.nextLong() & Long.MAX_VALUE);
        subscriptions.put(id, s);
        return id;
    }

    /** Until when a subscription lives; null when there is none by that id (it ended, or never was). */
    synchronized Instant expires(String id, Instant now) {
        Subscription s = live(id, now);
        return s == null ? null : s.expires;
    }

    /**
     * The news for a subscription, at most `limit` of it, waiting up to `wait` (and MAX_WAIT) while there is none. Null when there is
     * no such subscription. Asking keeps the subscription alive.
     */
    synchronized Pulled pull(String id, Duration wait, int limit, Instant now) {
        Subscription s = live(id, now);
        if (s == null) return null;
        long waitMs = wait == null ? MAX_WAIT.toMillis() : Math.max(0, Math.min(wait.toMillis(), MAX_WAIT.toMillis()));
        Instant alive = now.plusMillis(waitMs).plus(KEEP);
        if (alive.isAfter(s.expires)) s.expires = alive;
        long start = System.nanoTime();
        if (s.queue.isEmpty() && waitMs > 0 && waiting < MAX_WAITING) {
            waiting++;
            try {
                while (s.queue.isEmpty() && !closed && subscriptions.get(id) == s) {
                    long left = waitMs - (System.nanoTime() - start) / 1_000_000;
                    if (left <= 0) break;
                    wait(left);
                }
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
            } finally {
                waiting--;
            }
        }
        List<String> messages = new ArrayList<>();
        while (messages.size() < Math.max(1, limit) && !s.queue.isEmpty()) messages.add(s.queue.poll());
        return new Pulled(messages, now.plusNanos(System.nanoTime() - start), s.expires);
    }

    /** A subscription's new end, or null when there is no such subscription. */
    synchronized Instant renew(String id, Duration life, Instant now) {
        Subscription s = live(id, now);
        if (s == null) return null;
        s.expires = now.plus(life(life));
        return s.expires;
    }

    synchronized boolean unsubscribe(String id) {
        boolean was = subscriptions.remove(id) != null;
        notifyAll();
        return was;
    }

    /** Tell a subscription how things stand again (ONVIF's SetSynchronizationPoint). False when there is no such subscription. */
    synchronized boolean synchronize(String id, Instant now) {
        Subscription s = live(id, now);
        if (s == null) return false;
        queue(s, "Initialized", now);
        notifyAll();
        return true;
    }

    /** The camera is stopping: every waiting asking answers now, and none waits again. */
    synchronized void close() {
        closed = true;
        subscriptions.clear();
        notifyAll();
    }

    synchronized int size() {
        return subscriptions.size();
    }

    private Subscription live(String id, Instant now) {
        purge(now);
        return id == null ? null : subscriptions.get(id);
    }

    private void purge(Instant now) {
        for (Iterator<Subscription> i = subscriptions.values().iterator(); i.hasNext(); ) {
            if (!i.next().expires.isAfter(now)) i.remove();
        }
    }

    private void queue(Subscription s, String operation, Instant now) {
        s.queue.add(message(MOTION_TOPIC, motion, operation, now));
        s.queue.add(message(ALARM_TOPIC, motion, operation, now));
        while (s.queue.size() > MAX_QUEUED) s.queue.poll();
    }

    private static Duration life(Duration asked) {
        if (asked == null) return DEFAULT_LIFE;
        return asked.compareTo(MIN_LIFE) < 0 ? MIN_LIFE : asked.compareTo(MAX_LIFE) > 0 ? MAX_LIFE : asked;
    }

    /** One topic's state as a NotificationMessage: a property, so "Initialized" for how things stand and "Changed" for news. */
    static String message(String topic, boolean on, String operation, Instant when) {
        String source = topic.equals(MOTION_TOPIC)
                ? "<tt:SimpleItem Name=\"VideoSourceConfigurationToken\" Value=\"vsc\"/><tt:SimpleItem Name=\"VideoAnalyticsConfigurationToken\" "
                        + "Value=\"vac\"/><tt:SimpleItem Name=\"Rule\" Value=\"People\"/>"
                : "<tt:SimpleItem Name=\"Source\" Value=\"vs\"/>";
        String item = topic.equals(MOTION_TOPIC) ? "IsMotion" : "State";
        return "<wsnt:NotificationMessage><wsnt:Topic Dialect=\"" + DIALECT + "\">" + topic + "</wsnt:Topic><wsnt:Message><tt:Message UtcTime=\""
                + when.truncatedTo(ChronoUnit.MILLIS) + "\" PropertyOperation=\"" + operation + "\"><tt:Source>" + source + "</tt:Source><tt:Data>"
                + "<tt:SimpleItem Name=\"" + item + "\" Value=\"" + on + "\"/></tt:Data></tt:Message></wsnt:Message></wsnt:NotificationMessage>";
    }
}
