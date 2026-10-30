package to.croom.doorsign;

import java.io.File;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.util.HashMap;
import java.util.Map;
import java.util.function.Supplier;
import org.json.JSONException;
import org.json.JSONObject;

/**
 * The door sign's check-in with the room's TV Pi, every 5 seconds on a thread of its own (room settings spec 2026-10-29, section 7):
 * who it is, the version it runs, how it is connected, what it can't do, and its last job; the answer's job is handed to the JobRunner.
 * The panel calls the TV Pi; nothing listens on the panel. The sign itself goes on as it is whatever the check-in does.
 */
final class Agent {

    static final long RETRY_MS = 5_000;

    /** HTTP to the TV Pi (HttpURLConnection on the panel; a stand-in in the tests). */
    interface Http {
        /** POST `body` (JSON): the status and the body of the answer; IOException when there is none. */
        Response post(String url, Map<String, String> headers, byte[] body) throws IOException;

        /** GET `url` into `file`, at most `maxBytes`: the status (the file is whole only with 200); IOException when there is none. */
        int download(String url, Map<String, String> headers, File file, long maxBytes) throws IOException;

        /** GET `url`: whether it answered 200 within a few seconds. */
        boolean answers(String url);
    }

    static final class Response {
        final int status;
        final String body;

        Response(int status, String body) {
            this.status = status;
            this.body = body == null ? "" : body;
        }
    }

    /** What the panel says of itself in each check-in. */
    interface Device {
        long versionCode();

        String versionName();

        String name();

        JSONObject network();

        Map<String, String> cannot();
    }

    interface Log {
        void info(String words);
    }

    private final Supplier<String> server;
    private final Identity identity;
    private final Http http;
    private final Device device;
    private final JobRunner runner;
    private final Log log;
    private volatile String state = "Not checked in yet";
    private String trouble;

    /** @param server the room's address (scheme, host and port: "http://10.0.0.5:8080"), or null before the sign is set up. */
    Agent(Supplier<String> server, Identity identity, Http http, Device device, JobRunner runner, Log log) {
        this.server = server;
        this.identity = identity;
        this.http = http;
        this.device = device;
        this.runner = runner;
        this.log = log;
    }

    /** One check-in. The time to wait before the next, in milliseconds. */
    long once() {
        String base = server.get();
        if (base == null) {
            say("Not checked in: the sign has no Crystal Meet server yet");
            return RETRY_MS;
        }
        JSONObject body = CheckIn.body(identity, device.versionCode(), device.versionName(), device.name(), device.network(),
                runner.report(), device.cannot());
        Map<String, String> headers = new HashMap<>();
        headers.put("Content-Type", "application/json");
        headers.put(CheckIn.TOKEN_HEADER, identity.token);
        Response response;
        try {
            response = http.post(base + CheckIn.PATH, headers, body.toString().getBytes(StandardCharsets.UTF_8));
        } catch (IOException e) {
            say("Can't reach the TV Pi at " + base + ": " + e.getMessage());
            return RETRY_MS;
        }
        if (response.status != 200) {
            say("The TV Pi answered " + response.status + ": " + error(response.body));
            return RETRY_MS;
        }
        CheckIn.Answer answer;
        try {
            answer = CheckIn.parse(response.body);
        } catch (JSONException e) {
            say("The TV Pi's answer is not one to a check-in");
            return RETRY_MS;
        }
        if (!answer.allowed) {
            say("Waiting to join: tap Allow under Devices waiting to join on the touch Pi (Options › Settings › Software)");
        } else {
            say("Joined the room at " + base);
            if (answer.job != null && runner.take(answer.job)) log.info("Job " + answer.job.type + " (" + answer.job.id + ")");
        }
        return answer.intervalS * 1000L;
    }

    private static String error(String body) {
        try {
            return new JSONObject(body).optString("error", body);
        } catch (JSONException e) {
            return body.length() > 100 ? body.substring(0, 100) : body;
        }
    }

    private void say(String words) {
        state = words;
        if (!words.equals(trouble)) log.info(words);   // each new state once, not every 5 seconds
        trouble = words;
    }

    /** Where the check-in is, in words, for the setup screen. */
    String state() {
        return state;
    }
}
