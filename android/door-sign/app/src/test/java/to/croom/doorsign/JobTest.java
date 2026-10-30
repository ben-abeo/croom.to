package to.croom.doorsign;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertNull;
import static org.junit.Assert.assertTrue;
import static org.junit.Assert.fail;

import org.json.JSONObject;
import org.junit.Test;

public class JobTest {

    static final String SHA = "c".repeat(64);

    static Job job(String json) throws Exception {
        return Job.from(new JSONObject(json));
    }

    static String refusal(Job job) {
        try {
            job.check();
        } catch (Refused refused) {
            return refused.words();
        }
        fail("not refused");
        return null;
    }

    @Test public void aJobHasAnIdOf16HexAndAType() throws Exception {
        assertNull(Job.from("reboot"));
        assertNull(job("{\"id\": \"x\", \"type\": \"reboot\"}"));
        assertNull(job("{\"id\": \"0000000000000001\"}"));
        Job reboot = job("{\"id\": \"0000000000000001\", \"type\": \"reboot\", \"args\": {}}");
        reboot.check();
        assertEquals("reboot", reboot.type);
        assertEquals("The door sign does not know the job format", refusal(job("{\"id\": \"0000000000000001\", \"type\": \"format\"}")));
    }

    @Test public void anUpdateIsAVersionCodeWithItsSizeAndSha256NeverACommit() throws Exception {
        Job update = job("{\"id\": \"0000000000000002\", \"type\": \"update\", \"args\": {\"version_code\": 4, \"sha256\": \"" + SHA
                + "\", \"size\": 1234}}");
        update.check();
        assertEquals(4, update.versionCode());
        assertEquals(SHA, update.sha256());
        assertEquals(1234L, update.size());
        assertEquals("A door sign is updated to a release of the app (a version code), not to a commit",
                refusal(job("{\"id\": \"0000000000000002\", \"type\": \"update\", \"args\": {\"commit\": \"" + "b".repeat(40) + "\"}}")));
        for (String args : new String[] {"{}", "{\"version_code\": 0, \"sha256\": \"" + SHA + "\", \"size\": 1}",
                "{\"version_code\": \"4\", \"sha256\": \"" + SHA + "\", \"size\": 1}", "{\"version_code\": 4.5, \"sha256\": \"" + SHA + "\", \"size\": 1}",
                "{\"version_code\": 4, \"size\": 1}", "{\"version_code\": 4, \"sha256\": \"" + SHA.toUpperCase() + "\", \"size\": 1}",
                "{\"version_code\": 4, \"sha256\": \"" + SHA + "\"}", "{\"version_code\": 4, \"sha256\": \"" + SHA + "\", \"size\": 0}",
                "{\"version_code\": 3000000000, \"sha256\": \"" + SHA + "\", \"size\": 1}"}) {
            refusal(job("{\"id\": \"0000000000000002\", \"type\": \"update\", \"args\": " + args + "}"));
        }
    }

    @Test public void wifiArgumentsAreCheckedAgain() throws Exception {
        Job connect = job("{\"id\": \"0000000000000003\", \"type\": \"wifi_connect\", \"args\": {\"ssid\": \"Office\", \"password\": \"correct horse\"}}");
        connect.check();
        assertEquals("Office", connect.ssid());
        assertEquals("correct horse", connect.password());
        assertFalse(connect.hidden());
        Job open = job("{\"id\": \"0000000000000003\", \"type\": \"wifi_connect\", \"args\": {\"ssid\": \"Café\", \"hidden\": true}}");
        open.check();
        assertEquals("", open.password());
        assertTrue(open.hidden());
        assertEquals("Choose a network (a name of 1 to 32 characters)",
                refusal(job("{\"id\": \"0000000000000003\", \"type\": \"wifi_forget\", \"args\": {\"ssid\": \"" + "x".repeat(33) + "\"}}")));
        for (String args : new String[] {"{\"ssid\": \"\"}", "{\"ssid\": \"   \"}", "{\"ssid\": \"a\\u0007b\"}", "{\"ssid\": \"Office\", \"password\": \"short\"}",
                "{\"ssid\": \"Office\", \"password\": \"" + "x".repeat(64) + "\"}", "{\"ssid\": \"Office\", \"hidden\": \"yes\"}", "{\"ssid\": 3}"}) {
            refusal(job("{\"id\": \"0000000000000003\", \"type\": \"wifi_connect\", \"args\": " + args + "}"));
        }
        job("{\"id\": \"0000000000000003\", \"type\": \"wifi_connect\", \"args\": {\"ssid\": \"Office\", \"password\": \"" + "aB".repeat(32) + "\"}}").check();
    }
}
