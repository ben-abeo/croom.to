package to.croom.doorsign;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertNull;
import static org.junit.Assert.assertTrue;

import java.io.File;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.security.SecureRandom;
import java.util.ArrayList;
import java.util.Collections;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import org.json.JSONObject;
import org.junit.Before;
import org.junit.Test;

public class AgentTest {

    /** The TV Pi as a stand-in: each POST is answered with the next of `answers` (or fails with `down`); `posts` keeps what was sent. */
    static final class FakeHttp implements Agent.Http {
        final List<String> urls = new ArrayList<>();
        final List<Map<String, String>> headers = new ArrayList<>();
        final List<JSONObject> bodies = new ArrayList<>();
        final List<Agent.Response> answers = new ArrayList<>();
        IOException down;

        @Override public Agent.Response post(String url, Map<String, String> headers, byte[] body) throws IOException {
            urls.add(url);
            this.headers.add(headers);
            try {
                bodies.add(new JSONObject(new String(body, StandardCharsets.UTF_8)));
            } catch (Exception e) {
                throw new IllegalStateException(e);
            }
            if (down != null) throw down;
            return answers.isEmpty() ? new Agent.Response(200, "{\"state\": \"allowed\", \"job\": null, \"interval\": 5}") : answers.remove(0);
        }

        @Override public int download(String url, Map<String, String> headers, File file, long maxBytes) {
            return 404;
        }

        @Override public boolean answers(String url) {
            return true;
        }
    }

    static final class Device implements Agent.Device {
        final Map<String, String> cannot = new LinkedHashMap<>();

        @Override public long versionCode() {
            return 2;
        }

        @Override public String versionName() {
            return "2.0";
        }

        @Override public String name() {
            return "SMT1019";
        }

        @Override public JSONObject network() {
            try {
                return new JSONObject().put("ssid", "Office").put("connection", "wifi");
            } catch (Exception e) {
                throw new IllegalStateException(e);
            }
        }

        @Override public Map<String, String> cannot() {
            return cannot;
        }
    }

    FakeHttp http;
    Device device;
    JobRunnerTest.FakePanel panel;
    JobRunner runner;
    List<Runnable> queued;
    List<String> log;
    String server;
    Identity identity;
    Agent agent;

    @Before public void setUp() {
        http = new FakeHttp();
        device = new Device();
        panel = new JobRunnerTest.FakePanel();
        queued = new ArrayList<>();
        runner = new JobRunner(new MapStore(), panel, queued::add, () -> 0L);
        log = new ArrayList<>();
        server = "http://10.0.0.5:8080";
        identity = Identity.load(new MapStore(), new SecureRandom());
        agent = new Agent(() -> server, identity, http, device, runner, log::add);
    }

    @Test public void aCheckInSaysWhoItIsWithItsTokenInTheHeader() throws Exception {
        device.cannot.put("reboot", Panel.NOT_OWNER);
        http.answers.add(new Agent.Response(200, "{\"state\": \"pending\", \"interval\": 5}"));
        assertEquals(5_000, agent.once());
        assertEquals(List.of("http://10.0.0.5:8080/api/devices/checkin"), http.urls);
        assertEquals(identity.token, http.headers.get(0).get("X-Croom-Device-Token"));
        assertEquals("application/json", http.headers.get(0).get("Content-Type"));
        JSONObject body = http.bodies.get(0);
        assertEquals(identity.id, body.getString("id"));
        assertEquals("sign", body.getString("kind"));
        assertEquals("2", body.getString("version"));
        assertEquals("Office", body.getJSONObject("network").getString("ssid"));
        assertEquals(Panel.NOT_OWNER, body.getJSONObject("cannot").getString("reboot"));
        assertTrue(agent.state().startsWith("Waiting to join: tap Allow"));
    }

    @Test public void anAllowedPanelTakesItsJobAndReportsIt() throws Exception {
        http.answers.add(new Agent.Response(200, "{\"state\": \"allowed\", \"job\": {\"id\": \"0000000000000001\", \"type\": \"wifi_forget\", "
                + "\"args\": {\"ssid\": \"Guest\"}}, \"interval\": 5}"));
        agent.once();
        assertEquals(1, queued.size());
        queued.remove(0).run();
        agent.once();
        JSONObject job = http.bodies.get(1).getJSONObject("job");
        assertEquals("0000000000000001", job.getString("id"));
        assertEquals("done", job.getString("state"));
        assertEquals("Forgot Guest", job.getString("words"));
        assertEquals("Joined the room at http://10.0.0.5:8080", agent.state());
    }

    @Test public void troubleIsLoggedOnceAndTriedAgainIn5Seconds() {
        http.down = new IOException("connect timed out");
        assertEquals(5_000, agent.once());
        assertEquals(5_000, agent.once());
        assertEquals(List.of("Can't reach the TV Pi at http://10.0.0.5:8080: connect timed out"), log);
        http.down = null;
        http.answers.add(new Agent.Response(403, "{\"error\": \"This device was refused\"}"));
        assertEquals(5_000, agent.once());
        assertEquals("The TV Pi answered 403: This device was refused", agent.state());
        http.answers.add(new Agent.Response(200, "<html>"));
        assertEquals(5_000, agent.once());
        assertEquals("The TV Pi's answer is not one to a check-in", agent.state());
    }

    @Test public void noServerNoCheckIn() {
        server = null;
        assertEquals(5_000, agent.once());
        assertTrue(http.urls.isEmpty());
        assertFalse(agent.state().isEmpty());
        assertNull(runner.report());
        assertEquals(Collections.emptyList(), queued);
    }
}
