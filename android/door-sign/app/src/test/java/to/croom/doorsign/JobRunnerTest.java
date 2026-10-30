package to.croom.doorsign;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertNull;
import static org.junit.Assert.assertTrue;

import java.util.ArrayList;
import java.util.List;
import org.json.JSONArray;
import org.json.JSONObject;
import org.junit.Before;
import org.junit.Test;

public class JobRunnerTest {

    static final String SHA = "c".repeat(64);

    /** The panel's abilities, scripted: `cannot` answers for every job type, `refuse` makes the next job fail with those words. */
    static class FakePanel implements JobRunner.Abilities {
        final List<String> did = new ArrayList<>();
        String cannot;
        String refuse;
        long installed = 2;
        JSONObject wifiRecord;
        JobRunner.Progress progress;

        @Override public String cannot(String type) {
            return cannot;
        }

        private void maybeRefuse() throws Refused {
            if (refuse != null) throw new Refused(refuse);
        }

        @Override public void reboot() throws Refused {
            did.add("reboot");
            maybeRefuse();
        }

        @Override public JSONObject scan() throws Refused {
            did.add("scan");
            maybeRefuse();
            try {
                return new JSONObject().put("networks", new JSONArray().put(new JSONObject().put("ssid", "Office")).put(new JSONObject().put("ssid", "Guest")));
            } catch (Exception e) {
                throw new IllegalStateException(e);
            }
        }

        @Override public String connect(String ssid, String password, boolean hidden, String jobId, JobRunner.Progress progress) throws Refused {
            did.add("connect " + ssid + " " + password + " " + hidden + " " + jobId);
            this.progress = progress;
            progress.say("Joining " + ssid);
            maybeRefuse();
            return "Joined " + ssid + ". The door sign reaches the TV Pi";
        }

        @Override public String forget(String ssid) throws Refused {
            did.add("forget " + ssid);
            maybeRefuse();
            return "Forgot " + ssid;
        }

        @Override public String update(int versionCode, String sha256, long size, JobRunner.Progress progress) throws Refused {
            did.add("update " + versionCode + " " + sha256 + " " + size);
            progress.say("Installing version " + versionCode);
            maybeRefuse();
            return "Updated to version " + versionCode;
        }

        @Override public long installedVersion() {
            return installed;
        }

        @Override public JSONObject wifiOutcome(String jobId) {
            return wifiRecord;
        }
    }

    final List<Runnable> queued = new ArrayList<>();
    MapStore store;
    FakePanel panel;
    long now;
    JobRunner runner;

    @Before public void setUp() {
        store = new MapStore();
        panel = new FakePanel();
        now = 1_000;
        runner = new JobRunner(store, panel, queued::add, () -> now);
    }

    static Job job(String id, String type, String args) throws Exception {
        return Job.from(new JSONObject("{\"id\": \"" + id + "\", \"type\": \"" + type + "\", \"args\": " + args + "}"));
    }

    void work() {
        while (!queued.isEmpty()) queued.remove(0).run();
    }

    JSONObject report() {
        return runner.report();
    }

    @Test public void aJobRunsOnItsThreadAndIsReportedRunningThenDone() throws Exception {
        assertNull(report());
        assertTrue(runner.take(job("0000000000000001", "wifi_forget", "{\"ssid\": \"Guest\"}")));
        assertEquals("running", report().getString("state"));
        assertEquals("0000000000000001", report().getString("id"));
        assertEquals("wifi_forget", report().getString("type"));
        assertTrue(runner.busy());
        work();
        assertEquals("done", report().getString("state"));
        assertEquals("Forgot Guest", report().getString("words"));
        assertFalse(report().has("args"));
        assertEquals(List.of("forget Guest"), panel.did);
    }

    @Test public void oneAtATimeAndNeverTheSameJobTwice() throws Exception {
        Job first = job("0000000000000001", "reboot", "{}");
        assertTrue(runner.take(first));
        assertFalse(runner.take(job("0000000000000002", "wifi_scan", "{}")));     // the TV Pi hands it out again later
        work();
        assertFalse(runner.take(first));                                          // its answer was lost: handed out again
        assertEquals(List.of("reboot"), panel.did);
        assertTrue(runner.take(job("0000000000000002", "wifi_scan", "{}")));
    }

    @Test public void aJobThePanelCantDoIsAnsweredInItsWords() throws Exception {
        panel.cannot = Panel.NOT_OWNER;
        assertTrue(runner.take(job("0000000000000001", "reboot", "{}")));
        assertTrue(queued.isEmpty());
        assertEquals("failed", report().getString("state"));
        assertEquals(Panel.NOT_OWNER, report().getString("words"));
        assertTrue(panel.did.isEmpty());
    }

    @Test public void aJobWhoseArgumentsAreWrongIsRefused() throws Exception {
        runner.take(job("0000000000000001", "update", "{\"commit\": \"" + "b".repeat(40) + "\"}"));
        assertEquals("A door sign is updated to a release of the app (a version code), not to a commit", report().getString("words"));
        assertEquals("failed", report().getString("state"));
        runner.take(job("0000000000000002", "format_disk", "{}"));
        assertEquals("The door sign does not know the job format_disk", report().getString("words"));
        assertTrue(panel.did.isEmpty());
    }

    @Test public void aJobThatFailsSaysWhy() throws Exception {
        panel.refuse = "The door sign is on Office: join another network first, or it would be cut off";
        runner.take(job("0000000000000001", "wifi_forget", "{\"ssid\": \"Office\"}"));
        work();
        assertEquals("failed", report().getString("state"));
        assertEquals(panel.refuse, report().getString("words"));
        assertFalse(runner.busy());
    }

    @Test public void aScansNetworksGoWithTheCheckInsForAMinute() throws Exception {
        runner.take(job("0000000000000001", "wifi_scan", "{}"));
        work();
        assertEquals("Found 2 networks", report().getString("words"));
        assertEquals(2, report().getJSONObject("result").getJSONArray("networks").length());
        assertFalse(store.get(JobRunner.KEY).contains("Guest"));
        now += JobRunner.RESULT_FOR_MS + 1;
        assertFalse(report().has("result"));
    }

    @Test public void theWifiPasswordIsNeverKeptNorReported() throws Exception {
        runner.take(job("0000000000000001", "wifi_connect", "{\"ssid\": \"New Net\", \"password\": \"secret-words\", \"hidden\": false}"));
        assertEquals("running", report().getString("state"));
        work();
        assertEquals(List.of("connect New Net secret-words false 0000000000000001"), panel.did);
        assertEquals("Joined New Net. The door sign reaches the TV Pi", report().getString("words"));
        assertFalse(store.get(JobRunner.KEY).contains("secret-words"));
        assertFalse(report().toString().contains("secret-words"));
    }

    @Test public void progressShowsWhileItRuns() throws Exception {
        List<String> seen = new ArrayList<>();
        JobRunner.Abilities watching = new FakePanel() {
            @Override public String update(int versionCode, String sha256, long size, JobRunner.Progress progress) throws Refused {
                progress.say("Installing version " + versionCode);
                seen.add(store.get(JobRunner.KEY));
                return "Updated to version " + versionCode;
            }
        };
        runner = new JobRunner(store, watching, queued::add, () -> now);
        runner.take(job("0000000000000001", "update", "{\"version_code\": 3, \"sha256\": \"" + SHA + "\", \"size\": 10}"));
        work();
        JSONObject kept = new JSONObject(seen.get(0));
        assertEquals("running", kept.getString("state"));
        assertEquals("Installing version 3", kept.getString("words"));
        assertEquals(3, kept.getJSONObject("args").getInt("version_code"));    // what the next start compares the app's version with
    }

    // --- after a restart

    JobRunner restarted() {
        return new JobRunner(store, panel, queued::add, () -> now);
    }

    @Test public void aRebootIsReportedAtTheNextStart() throws Exception {
        runner.take(job("0000000000000001", "reboot", "{}"));
        work();
        assertEquals("running", report().getString("state"));      // until the panel goes down
        assertEquals("Rebooting", report().getString("words"));
        assertFalse(runner.busy());
        JobRunner after = restarted();
        after.resume();
        assertEquals("done", after.report().getString("state"));
        assertEquals("Rebooted", after.report().getString("words"));
        assertEquals("0000000000000001", after.report().getString("id"));
    }

    @Test public void aRebootAndroidRefusesSaysSo() throws Exception {
        panel.refuse = "Android did not reboot the panel: a call is ongoing";
        runner.take(job("0000000000000001", "reboot", "{}"));
        work();
        assertEquals("failed", report().getString("state"));
    }

    void updateCutOff() throws Exception {
        runner = new JobRunner(store, panel, r -> { }, () -> now);     // the app ends while it installs: the job never comes back
        runner.take(job("0000000000000001", "update", "{\"version_code\": 3, \"sha256\": \"" + SHA + "\", \"size\": 10}"));
    }

    @Test public void anUpdateThatTookIsReportedByTheNewVersion() throws Exception {
        updateCutOff();
        panel.installed = 3;
        JobRunner after = restarted();
        after.resume();
        assertEquals("done", after.report().getString("state"));
        assertEquals("Updated to version 3", after.report().getString("words"));
    }

    @Test public void anUpdateThatDidNotTakeSaysTheVersionItIsStillOn() throws Exception {
        updateCutOff();
        JobRunner after = restarted();
        after.resume();
        assertEquals("failed", after.report().getString("state"));
        assertEquals("The update to version 3 did not finish: the door sign is still on version 2", after.report().getString("words"));
    }

    @Test public void aWifiChangeCutOffHasItsRecordsResult() throws Exception {
        runner = new JobRunner(store, panel, r -> { }, () -> now);
        runner.take(job("0000000000000001", "wifi_connect", "{\"ssid\": \"New Net\", \"password\": \"secret-words\"}"));
        panel.wifiRecord = new JSONObject().put("state", "undone").put("job", "0000000000000001")
                .put("words", "The door sign restarted before New Net was proven. The door sign is back on Office");
        JobRunner after = restarted();
        after.resume();
        assertEquals("failed", after.report().getString("state"));
        assertEquals("The door sign restarted before New Net was proven. The door sign is back on Office", after.report().getString("words"));
    }

    @Test public void anyOtherJobCutOffStoppedPartWay() throws Exception {
        runner = new JobRunner(store, panel, r -> { }, () -> now);
        runner.take(job("0000000000000001", "wifi_scan", "{}"));
        JobRunner after = restarted();
        after.resume();
        assertEquals("The door sign restarted before it finished", after.report().getString("words"));
        // A finished job is left as it was
        int writes = store.writes;
        restarted().resume();
        assertEquals(writes, store.writes);
    }
}
