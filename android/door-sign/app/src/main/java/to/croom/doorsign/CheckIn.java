package to.croom.doorsign;

import java.util.Map;
import org.json.JSONException;
import org.json.JSONObject;

/**
 * The check-in with the room's TV Pi (room settings spec 2026-10-29, section 7): POST /api/devices/checkin every 5 seconds, JSON
 * {"id", "kind": "sign", "version" (the app's versionCode), "version_name", "name", "network", "job" (the last job's state), "cannot"
 * (the jobs this panel can't do, and why)}, with the token in the header X-Croom-Device-Token. The answer is {"state": "pending"} until
 * Allow on Settings › Software, then {"state": "allowed", "job": the next job or null, "interval"}.
 */
final class CheckIn {

    static final String PATH = "/api/devices/checkin";
    static final String TOKEN_HEADER = "X-Croom-Device-Token";
    static final String ID_HEADER = "X-Croom-Device-Id";
    static final String KIND = "sign";
    static final int INTERVAL_S = 5;

    private CheckIn() {}

    static JSONObject body(Identity identity, long versionCode, String versionName, String name, JSONObject network, JSONObject job,
                           Map<String, String> cannot) {
        try {
            JSONObject body = new JSONObject();
            body.put("id", identity.id);
            body.put("kind", KIND);
            body.put("version", String.valueOf(versionCode));
            body.put("version_name", versionName == null ? "" : versionName);
            body.put("name", name == null ? "" : name);
            body.put("network", network == null ? new JSONObject() : network);
            body.put("job", job == null ? JSONObject.NULL : job);
            JSONObject reasons = new JSONObject();
            for (Map.Entry<String, String> entry : cannot.entrySet()) reasons.put(entry.getKey(), entry.getValue());
            body.put("cannot", reasons);
            return body;
        } catch (JSONException e) {
            throw new IllegalStateException(e);
        }
    }

    /** What the TV Pi answered. */
    static final class Answer {
        final boolean allowed;
        final Job job;          // null: none (or one that is not a job at all)
        final int intervalS;

        Answer(boolean allowed, Job job, int intervalS) {
            this.allowed = allowed;
            this.job = job;
            this.intervalS = intervalS;
        }
    }

    /** The answer's JSON; JSONException when it is not an answer to a check-in. */
    static Answer parse(String text) throws JSONException {
        JSONObject answer = new JSONObject(text);
        String state = answer.getString("state");
        if (!state.equals("pending") && !state.equals("allowed")) throw new JSONException("not a check-in state: " + state);
        int interval = answer.optInt("interval", INTERVAL_S);
        interval = Math.max(2, Math.min(60, interval));
        boolean allowed = state.equals("allowed");
        return new Answer(allowed, allowed ? Job.from(answer.opt("job")) : null, interval);
    }
}
