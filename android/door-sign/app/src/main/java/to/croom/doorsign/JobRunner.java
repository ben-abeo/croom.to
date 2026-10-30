package to.croom.doorsign;

import java.util.concurrent.Executor;
import java.util.function.LongSupplier;
import org.json.JSONException;
import org.json.JSONObject;

/**
 * The door sign's jobs, one at a time (room settings spec 2026-10-29, section 7). A job the TV Pi hands out is checked, then run on its
 * own thread; each check-in tells the TV Pi its state ({"id", "type", "state": running | done | failed, "words", and a scan's "result"}).
 * The last job is kept in the app's storage, so that one the panel did not live through (a reboot, an update, which Android ends by
 * restarting the app with the new version) is reported at the next start: "Rebooted", "Updated to version 4", or why not.
 *
 * A job this panel can't do (its app is not the device owner) is answered at once in the panel's words. A job id seen before is not run
 * again (the TV Pi hands out a job again when its answer was lost), nor is a job taken while another runs.
 */
final class JobRunner {

    static final String KEY = "job";
    static final long RESULT_FOR_MS = 60_000;     // a scan's networks go with the check-ins this long after it (the TV Pi keeps them)
    static final String RUNNING = "running";
    static final String DONE = "done";
    static final String FAILED = "failed";

    interface Progress {
        void say(String words);
    }

    /** What the panel can do. Each answers its words of success, or Refused with the words of why not. */
    interface Abilities {
        /** Null when the panel can do jobs of `type`; otherwise why not ("The door sign is not the panel's device owner…"). */
        String cannot(String type);

        /** Reboot the panel (called after "Rebooting" is kept: it may never return). */
        void reboot() throws Refused;

        /** The networks in range: {"networks": [...]}. */
        JSONObject scan() throws Refused;

        String connect(String ssid, String password, boolean hidden, String jobId, Progress progress) throws Refused;

        String forget(String ssid) throws Refused;

        /** Download, check and install release `versionCode` (Android ends the app when the install takes; it may never return). */
        String update(int versionCode, String sha256, long size, Progress progress) throws Refused;

        /** The versionCode of the app installed now. */
        long installedVersion();

        /** The Wi-Fi change of job `jobId` as the panel's record has it after a restart: {state (done|undone), words}, or null. */
        JSONObject wifiOutcome(String jobId);
    }

    private final Store store;
    private final Abilities abilities;
    private final Executor executor;
    private final LongSupplier clock;
    private JSONObject job;            // the last job: what each check-in says
    private boolean running;
    private long endedAt;

    JobRunner(Store store, Abilities abilities, Executor executor, LongSupplier clock) {
        this.store = store;
        this.abilities = abilities;
        this.executor = executor;
        this.clock = clock;
        this.job = read(store.get(KEY));
    }

    private static JSONObject read(String text) {
        if (text == null) return null;
        try {
            JSONObject job = new JSONObject(text);
            return job.optString("id").matches("[0-9a-f]{16}") ? job : null;
        } catch (JSONException e) {
            return null;
        }
    }

    /**
     * At the app's start: a job kept as running did not finish in this app's life. A reboot did ("Rebooted"); an update did when the
     * app now is that version; a Wi-Fi change has its record's result; anything else stopped part way.
     */
    synchronized void resume() {
        if (job == null || !RUNNING.equals(job.optString("state"))) return;
        String type = job.optString("type");
        if (Job.REBOOT.equals(type)) {
            end(DONE, "Rebooted", null);
        } else if (Job.UPDATE.equals(type)) {
            long wanted = job.optJSONObject("args") == null ? 0 : job.optJSONObject("args").optLong("version_code");
            long installed = abilities.installedVersion();
            if (wanted > 0 && installed >= wanted) end(DONE, "Updated to version " + installed, null);
            else end(FAILED, "The update to version " + wanted + " did not finish: the door sign is still on version " + installed, null);
        } else if (Job.WIFI_CONNECT.equals(type)) {
            JSONObject outcome = abilities.wifiOutcome(job.optString("id"));
            if (outcome != null) end("done".equals(outcome.optString("state")) ? DONE : FAILED, outcome.optString("words"), null);
            else end(FAILED, "The door sign restarted before it finished", null);
        } else {
            end(FAILED, "The door sign restarted before it finished", null);
        }
    }

    /** Take a job the TV Pi handed out. False when it is not taken (seen before, or another job runs). */
    synchronized boolean take(Job next) {
        if (next == null || running) return false;
        if (job != null && next.id.equals(job.optString("id"))) return false;
        job = new JSONObject();
        try {
            job.put("id", next.id);
            job.put("type", next.type);
            JSONObject args = new JSONObject();
            if (Job.UPDATE.equals(next.type) && next.args.opt("version_code") instanceof Number) {
                args.put("version_code", next.args.opt("version_code"));
            }
            if (next.args.opt("ssid") instanceof String) args.put("ssid", next.args.opt("ssid"));   // never the password
            job.put("args", args);
        } catch (JSONException e) {
            throw new IllegalStateException(e);
        }
        try {
            next.check();
        } catch (Refused refused) {
            end(FAILED, refused.words(), null);
            return true;
        }
        String cannot = abilities.cannot(next.type);
        if (cannot != null) {
            end(FAILED, cannot, null);
            return true;
        }
        running = true;
        set(RUNNING, "", null);
        executor.execute(() -> run(next));
        return true;
    }

    private void run(Job next) {
        try {
            switch (next.type) {
                case Job.REBOOT:
                    progress("Rebooting");
                    abilities.reboot();
                    released();       // kept as running "Rebooting": the next start says "Rebooted"
                    break;
                case Job.WIFI_SCAN:
                    progress("Looking for networks");
                    JSONObject result = abilities.scan();
                    int found = result.optJSONArray("networks") == null ? 0 : result.optJSONArray("networks").length();
                    finish(DONE, "Found " + found + (found == 1 ? " network" : " networks"), result);
                    break;
                case Job.WIFI_CONNECT:
                    finish(DONE, abilities.connect(next.ssid(), next.password(), next.hidden(), next.id, this::progress), null);
                    break;
                case Job.WIFI_FORGET:
                    finish(DONE, abilities.forget(next.ssid()), null);
                    break;
                case Job.UPDATE:
                    progress("Downloading version " + next.versionCode());
                    finish(DONE, abilities.update(next.versionCode(), next.sha256(), next.size(), this::progress), null);
                    break;
                default:
                    finish(FAILED, "The door sign does not know the job " + next.type, null);
            }
        } catch (Refused refused) {
            finish(FAILED, refused.words(), null);
        } catch (RuntimeException e) {
            finish(FAILED, "The door sign could not do it: " + e, null);
        }
    }

    private synchronized void released() {
        running = false;
    }

    private synchronized void progress(String words) {
        if (running) set(RUNNING, words, null);
    }

    private synchronized void finish(String state, String words, JSONObject result) {
        running = false;
        end(state, words, result);
    }

    private void end(String state, String words, JSONObject result) {
        endedAt = clock.getAsLong();
        set(state, words, result);
    }

    private void set(String state, String words, JSONObject result) {
        try {
            job.put("state", state);
            job.put("words", words == null ? "" : words);
            if (result != null) job.put("result", result);
            else job.remove("result");
        } catch (JSONException e) {
            throw new IllegalStateException(e);
        }
        JSONObject kept = copy(job);
        kept.remove("result");          // a scan's networks are not kept: they are for the TV Pi now
        store.put(KEY, kept.toString());
    }

    private static JSONObject copy(JSONObject job) {
        try {
            return new JSONObject(job.toString());
        } catch (JSONException e) {
            throw new IllegalStateException(e);
        }
    }

    /** What the next check-in says of the last job ({"id", "type", "state", "words", and "result" for a scan's first minute}), or null. */
    synchronized JSONObject report() {
        if (job == null) return null;
        JSONObject report = copy(job);
        report.remove("args");
        if (!running && clock.getAsLong() - endedAt > RESULT_FOR_MS) report.remove("result");
        return report;
    }

    synchronized boolean busy() {
        return running;
    }
}
