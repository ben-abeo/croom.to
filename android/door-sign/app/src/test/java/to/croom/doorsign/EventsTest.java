package to.croom.doorsign;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertNotNull;
import static org.junit.Assert.assertNull;
import static org.junit.Assert.assertTrue;

import java.time.Duration;
import java.time.Instant;
import java.util.List;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicReference;
import org.junit.Test;

public class EventsTest {

    static final Instant NOW = Instant.parse("2026-11-22T18:00:00Z");
    static final Duration AT_ONCE = Duration.ZERO;

    static List<String> news(Events events, String id) {
        return events.pull(id, AT_ONCE, 100, NOW).messages;
    }

    @Test public void aNewSubscriptionIsToldHowThingsStandOnBothTopics() {
        Events events = new Events();
        events.motion(true, NOW);
        List<String> first = news(events, events.subscribe(null, NOW));
        assertEquals(2, first.size());
        assertTrue(first.get(0).contains(">tns1:RuleEngine/CellMotionDetector/Motion<") && first.get(0).contains("Name=\"IsMotion\" Value=\"true\""));
        assertTrue(first.get(1).contains(">tns1:VideoSource/MotionAlarm<") && first.get(1).contains("Name=\"State\" Value=\"true\""));
        for (String message : first) assertTrue(message.contains("PropertyOperation=\"Initialized\""));
    }

    @Test public void aChangeIsToldOnceAndOnlyWhenItIsOne() {
        Events events = new Events();
        String id = events.subscribe(null, NOW);
        news(events, id);
        events.motion(false, NOW);                      // nobody was there, and nobody is: no news
        assertTrue(news(events, id).isEmpty());
        events.motion(true, NOW.plusSeconds(1));
        events.motion(true, NOW.plusSeconds(2));
        List<String> came = news(events, id);
        assertEquals(2, came.size());
        assertTrue(came.get(0).contains("PropertyOperation=\"Changed\"") && came.get(0).contains("Value=\"true\""));
        assertTrue(came.get(0).contains("UtcTime=\"2026-11-22T18:00:01Z\""));
        events.motion(false, NOW.plusSeconds(30));
        assertTrue(news(events, id).get(1).contains("Name=\"State\" Value=\"false\""));
        assertTrue(news(events, id).isEmpty());
    }

    @Test public void everySubscriberIsTold() {
        Events events = new Events();
        String a = events.subscribe(null, NOW);
        String b = events.subscribe(null, NOW);
        news(events, a);
        news(events, b);
        events.motion(true, NOW);
        assertEquals(2, news(events, a).size());
        assertEquals(2, news(events, b).size());
    }

    @Test public void theLimitHoldsTheRestForTheNextAsking() {
        Events events = new Events();
        String id = events.subscribe(null, NOW);
        assertEquals(1, events.pull(id, AT_ONCE, 1, NOW).messages.size());
        assertEquals(1, events.pull(id, AT_ONCE, 1, NOW).messages.size());
        assertTrue(news(events, id).isEmpty());
    }

    @Test public void anAskingWaitsForNews() throws Exception {
        Events events = new Events();
        String id = events.subscribe(null, NOW);
        news(events, id);
        AtomicReference<Events.Pulled> got = new AtomicReference<>();
        CountDownLatch done = new CountDownLatch(1);
        Thread asking = new Thread(() -> {
            got.set(events.pull(id, Duration.ofSeconds(30), 100, NOW));
            done.countDown();
        });
        asking.start();
        assertFalse(done.await(200, TimeUnit.MILLISECONDS));         // nothing to tell yet: it waits
        events.motion(true, NOW);
        assertTrue(done.await(5, TimeUnit.SECONDS));
        assertEquals(2, got.get().messages.size());
    }

    @Test public void anAskingWithNoNewsAnswersWhenItsTimeIsUp() {
        Events events = new Events();
        String id = events.subscribe(null, NOW);
        news(events, id);
        long start = System.nanoTime();
        Events.Pulled pulled = events.pull(id, Duration.ofMillis(150), 100, NOW);
        long tookMs = (System.nanoTime() - start) / 1_000_000;
        assertTrue(pulled.messages.isEmpty());
        assertTrue("waited " + tookMs + " ms", tookMs >= 140 && tookMs < 5_000);
        assertFalse(pulled.at.isBefore(NOW.plusMillis(140)));        // the answer's time is when it was given
    }

    @Test public void closingAnswersEveryWaitingAsking() throws Exception {
        Events events = new Events();
        String id = events.subscribe(null, NOW);
        news(events, id);
        CountDownLatch done = new CountDownLatch(1);
        new Thread(() -> {
            events.pull(id, Duration.ofSeconds(30), 100, NOW);
            done.countDown();
        }).start();
        assertFalse(done.await(100, TimeUnit.MILLISECONDS));
        events.close();
        assertTrue(done.await(5, TimeUnit.SECONDS));
        assertNull(events.pull(id, AT_ONCE, 100, NOW));
    }

    @Test public void onlySoManyAskingsWaitAtOnce() throws Exception {
        Events events = new Events();
        String id = events.subscribe(null, NOW);
        news(events, id);
        CountDownLatch done = new CountDownLatch(Events.MAX_WAITING);
        for (int i = 0; i < Events.MAX_WAITING; i++) {
            new Thread(() -> {
                events.pull(id, Duration.ofSeconds(30), 100, NOW);
                done.countDown();
            }).start();
        }
        Thread.sleep(200);                                           // they are all waiting now
        long start = System.nanoTime();
        assertTrue(events.pull(id, Duration.ofSeconds(30), 100, NOW).messages.isEmpty());      // one more is answered at once
        assertTrue((System.nanoTime() - start) / 1_000_000 < 2_000);
        events.close();
        assertTrue(done.await(5, TimeUnit.SECONDS));
    }

    @Test public void aSubscriptionEndsUnlessItIsAskedOrRenewed() {
        Events events = new Events();
        String id = events.subscribe(Duration.ofSeconds(60), NOW);
        assertEquals(NOW.plusSeconds(60), events.expires(id, NOW));
        assertNotNull(events.pull(id, AT_ONCE, 100, NOW.plusSeconds(59)));       // asking keeps it: a minute more
        assertNotNull(events.pull(id, AT_ONCE, 100, NOW.plusSeconds(118)));
        assertEquals(NOW.plusSeconds(300), events.renew(id, Duration.ofSeconds(123), NOW.plusSeconds(177)));
        assertNull(events.pull(id, AT_ONCE, 100, NOW.plusSeconds(300)));
        assertNull(events.renew(id, Duration.ofSeconds(60), NOW.plusSeconds(300)));
        assertEquals(0, events.size());
    }

    @Test public void aSubscriptionsLifeIsKeptWithinBounds() {
        Events events = new Events();
        assertEquals(NOW.plus(Events.MIN_LIFE), events.expires(events.subscribe(Duration.ofSeconds(1), NOW), NOW));
        assertEquals(NOW.plus(Events.MIN_LIFE), events.expires(events.subscribe(Duration.ofSeconds(-5), NOW), NOW));
        assertEquals(NOW.plus(Events.MAX_LIFE), events.expires(events.subscribe(Duration.ofDays(30), NOW), NOW));
        assertEquals(NOW.plus(Events.DEFAULT_LIFE), events.expires(events.subscribe(null, NOW), NOW));
    }

    @Test public void whenAllAreTakenTheOneNearestItsEndMakesRoom() {
        Events events = new Events();
        String shortest = events.subscribe(Duration.ofSeconds(20), NOW);
        for (int i = 1; i < Events.MAX_SUBSCRIPTIONS; i++) events.subscribe(Duration.ofSeconds(600), NOW);
        String last = events.subscribe(Duration.ofSeconds(600), NOW);
        assertEquals(Events.MAX_SUBSCRIPTIONS, events.size());
        assertNull(events.expires(shortest, NOW));
        assertNotNull(events.expires(last, NOW));
    }

    @Test public void aSubscriberThatStopsAskingLosesTheOldestNews() {
        Events events = new Events();
        String id = events.subscribe(Duration.ofHours(1), NOW);
        for (int i = 0; i < 200; i++) events.motion(i % 2 == 0, NOW.plusSeconds(i));
        assertEquals(Events.MAX_QUEUED, news(events, id).size());
    }

    @Test public void unsubscribingEndsItAndSynchronizingTellsItAgain() {
        Events events = new Events();
        String id = events.subscribe(null, NOW);
        news(events, id);
        assertTrue(events.synchronize(id, NOW));
        assertTrue(news(events, id).get(0).contains("PropertyOperation=\"Initialized\""));
        assertTrue(events.unsubscribe(id));
        assertFalse(events.unsubscribe(id));
        assertFalse(events.synchronize(id, NOW));
        assertNull(events.pull(id, AT_ONCE, 100, NOW));
        assertNull(events.pull(null, AT_ONCE, 100, NOW));
    }
}
