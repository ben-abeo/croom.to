package to.croom.doorsign;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertNull;
import static org.junit.Assert.assertTrue;
import static org.junit.Assert.fail;

import java.security.SecureRandom;
import java.util.LinkedHashMap;
import java.util.Map;
import org.json.JSONException;
import org.json.JSONObject;
import org.junit.Test;

public class CheckInTest {

    @Test public void theBodySaysWhoItIsAndWhatItCantDo() throws Exception {
        Identity identity = Identity.load(new MapStore(), new SecureRandom());
        Map<String, String> cannot = new LinkedHashMap<>();
        cannot.put("reboot", Panel.NOT_OWNER);
        JSONObject network = new JSONObject().put("ssid", "Office");
        JSONObject body = CheckIn.body(identity, 3, "3.0", "SMT1019", network, null, cannot);
        assertEquals(identity.id, body.getString("id"));
        assertEquals("sign", body.getString("kind"));
        assertEquals("3", body.getString("version"));       // the TV Pi compares it with the releases' version codes
        assertEquals("3.0", body.getString("version_name"));
        assertEquals("SMT1019", body.getString("name"));
        assertEquals("Office", body.getJSONObject("network").getString("ssid"));
        assertTrue(body.isNull("job"));
        assertEquals(Panel.NOT_OWNER, body.getJSONObject("cannot").getString("reboot"));
        assertFalse(body.toString().contains(identity.token));     // the token goes in the header only
    }

    @Test public void theAnswers() throws Exception {
        CheckIn.Answer pending = CheckIn.parse("{\"state\": \"pending\", \"interval\": 5}");
        assertFalse(pending.allowed);
        assertNull(pending.job);
        assertEquals(5, pending.intervalS);
        CheckIn.Answer allowed = CheckIn.parse("{\"state\": \"allowed\", \"job\": {\"id\": \"0000000000000001\", \"type\": \"reboot\", \"args\": {}}, \"interval\": 5}");
        assertTrue(allowed.allowed);
        assertEquals("reboot", allowed.job.type);
        assertEquals("0000000000000001", allowed.job.id);
        assertNull(CheckIn.parse("{\"state\": \"allowed\", \"job\": null}").job);
        assertEquals(60, CheckIn.parse("{\"state\": \"allowed\", \"job\": null, \"interval\": 3600}").intervalS);
        assertEquals(2, CheckIn.parse("{\"state\": \"allowed\", \"job\": null, \"interval\": 0}").intervalS);
        for (String bad : new String[] {"", "[]", "{}", "{\"state\": \"refused\"}", "<html>"}) {
            try {
                CheckIn.parse(bad);
                fail(bad);
            } catch (JSONException expected) {
                // not an answer to a check-in
            }
        }
    }
}
