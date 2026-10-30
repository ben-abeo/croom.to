package to.croom.doorsign;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertNull;
import static org.junit.Assert.assertTrue;
import static org.junit.Assert.fail;

import java.util.ArrayList;
import java.util.HashMap;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;
import org.json.JSONArray;
import org.json.JSONObject;
import org.junit.Before;
import org.junit.Test;

public class WifiChangeTest {

    static final class PowerCut extends RuntimeException {
    }

    /**
     * Android's Wi-Fi as a stand-in: the networks in the air (name, capabilities, signal, password), the saved ones, the one the panel
     * is on, and where the room's TV Pi answers. Joining takes at once when the network is in the air (or hidden) and the password is
     * right. `cutAt` ends everything (a power cut) at that numbered change (a save, a join, a removal, a record written).
     */
    final class FakeWifi implements WifiChange.WifiPort {
        final Map<String, String[]> air = new LinkedHashMap<>();      // ssid -> {capabilities, password (null: open)}
        final Map<String, Integer> signals = new HashMap<>();
        final Map<Integer, String[]> saved = new LinkedHashMap<>();    // id -> {ssid, password}
        final Set<Integer> allowed = new HashSet<>();
        final Set<String> roomOn = new HashSet<>();                   // the networks the TV Pi is reached on
        final Set<String> hiding = new HashSet<>();                   // in the air, but not in a scan: they answer to their name only
        final List<String> calls = new ArrayList<>();
        Integer on;
        int nextId = 10;
        boolean ethernet;
        boolean dropAfterFirstCheck;

        void inAir(String ssid, String capabilities, String password, int signal) {
            air.put(ssid, new String[] {capabilities, password});
            signals.put(ssid, signal);
        }

        int save(String ssid, String password) {
            int id = nextId++;
            saved.put(id, new String[] {ssid, password});
            allowed.add(id);
            return id;
        }

        void change(String call) {
            calls.add(call);
            step();
        }

        @Override public boolean hasWifi() {
            return true;
        }

        @Override public WifiChange.Current current() {
            return on == null ? null : new WifiChange.Current(on, saved.get(on)[0], signals.getOrDefault(saved.get(on)[0], 50));
        }

        @Override public List<WifiChange.Seen> scan(boolean fresh) {
            List<WifiChange.Seen> seen = new ArrayList<>();
            for (Map.Entry<String, String[]> n : air.entrySet()) if (!hiding.contains(n.getKey())) seen.add(new WifiChange.Seen(n.getKey(), signals.get(n.getKey()), n.getValue()[0]));
            return seen;
        }

        @Override public List<WifiChange.Saved> saved() {
            List<WifiChange.Saved> list = new ArrayList<>();
            for (Map.Entry<Integer, String[]> n : saved.entrySet()) list.add(new WifiChange.Saved(n.getKey(), n.getValue()[0]));
            return list;
        }

        @Override public int add(String ssid, String password, String kind, boolean hidden) {
            int id = nextId++;
            saved.put(id, new String[] {ssid, password});
            allowed.add(id);
            change("add " + ssid + " " + kind + (hidden ? " hidden" : ""));
            return id;
        }

        @Override public boolean connect(int networkId) {
            change("connect " + networkId);
            String[] network = saved.get(networkId);
            if (network == null) return false;
            String[] there = air.get(network[0]);
            boolean right = there != null && (there[1] == null ? network[1].isEmpty() : there[1].equals(network[1]));
            on = right ? Integer.valueOf(networkId) : null;
            return true;
        }

        @Override public void allow(int networkId) {
            allowed.add(networkId);
            change("allow " + networkId);
        }

        @Override public boolean remove(int networkId) {
            boolean had = saved.remove(networkId) != null;
            if (on != null && on == networkId) on = null;
            change("remove " + networkId);
            return had;
        }

        @Override public String rejected(int networkId) {
            return null;
        }

        @Override public boolean reachesRoom() {
            boolean reached = ethernet || on != null && roomOn.contains(saved.get(on)[0]);
            if (dropAfterFirstCheck) on = null;
            return reached;
        }

        @Override public boolean onEthernet() {
            return ethernet;
        }

        @Override public String address() {
            return on == null ? null : "10.0.0.20";
        }
    }

    /** The store, which may be cut off too. */
    final class CutStore extends MapStore {
        @Override public void put(String key, String value) {
            step();
            super.put(key, value);
        }
    }

    FakeWifi wifi;
    CutStore store;
    long now;
    int steps;
    int cutAt = -1;
    int office;

    void step() {
        steps++;
        if (steps == cutAt) throw new PowerCut();
    }

    @Before public void setUp() {
        wifi = new FakeWifi();
        store = new CutStore();
        now = 0;
        wifi.inAir("Office", "[WPA2-PSK-CCMP][RSN-PSK-CCMP][ESS]", "office-pass", 70);
        wifi.inAir("New Net", "[RSN-PSK-CCMP][ESS]", "new-password", 80);
        wifi.inAir("Guest", "[ESS]", null, 40);
        wifi.inAir("Corp", "[WPA2-EAP-CCMP][ESS]", "x", 60);
        wifi.inAir("Old", "[WEP][ESS]", "x", 30);
        wifi.inAir("Modern", "[RSN-SAE-CCMP][ESS]", "modern-pass", 30);
        office = wifi.save("Office", "office-pass");
        wifi.on = office;
        wifi.roomOn.add("Office");
        wifi.roomOn.add("New Net");
    }

    WifiChange change() {
        return new WifiChange(wifi, store, () -> now, ms -> {
            step();
            now += ms;
        }, () -> "2026-10-30T10:00:00Z");
    }

    static final JobRunner.Progress QUIET = words -> { };

    String refusal(ThrowingCall call) {
        try {
            call.run();
        } catch (Refused refused) {
            return refused.words();
        }
        fail("not refused");
        return null;
    }

    interface ThrowingCall {
        void run() throws Refused;
    }

    Set<String> savedNames() {
        Set<String> names = new HashSet<>();
        for (String[] n : wifi.saved.values()) names.add(n[0]);
        return names;
    }

    @Test public void aNewNetworkTheTvPiIsReachedOnIsKept() throws Exception {
        List<String> said = new ArrayList<>();
        String words = change().change("New Net", "new-password", false, "0000000000000001", said::add);
        assertEquals("Joined New Net. The door sign reaches the TV Pi", words);
        assertEquals("New Net", wifi.current().ssid);
        assertTrue(savedNames().contains("Office"));                 // remembered; Forget removes it
        assertTrue(wifi.calls.contains("allow " + office));
        assertEquals(List.of("Joining New Net", "Joining New Net: checking that the door sign reaches the TV Pi on it"), said);
        JSONObject record = change().record();
        assertEquals("done", record.getString("state"));
        assertEquals("0000000000000001", record.getString("job"));
        assertFalse(store.values.get(WifiChange.KEY).contains("new-password"));
        assertEquals("add New Net psk", wifi.calls.get(0));
    }

    @Test public void aNetworkTheTvPiIsNotReachedOnIsLeftWithin90Seconds() {
        wifi.roomOn.remove("New Net");
        String words = refusal(() -> change().change("New Net", "new-password", false, "j", QUIET));
        assertEquals("Joined New Net, but the door sign could not reach the TV Pi on it within 90 seconds. The door sign is back on Office", words);
        assertEquals(WifiChange.TRIAL_MS, now);
        assertEquals("Office", wifi.current().ssid);
        assertFalse(savedNames().contains("New Net"));
        assertEquals("undone", change().record().optString("state"));
    }

    @Test public void aWrongPasswordNeverJoinsAndGoesBack() {
        String words = refusal(() -> change().change("New Net", "wrong-password", false, "j", QUIET));
        assertEquals("Could not join New Net within 90 seconds. The door sign is back on Office", words);
        assertEquals(office, (int) wifi.on);
        assertFalse(savedNames().contains("New Net"));
    }

    @Test public void droppedAfterTheCheckIsNotKept() {
        wifi.dropAfterFirstCheck = true;
        String words = refusal(() -> change().change("New Net", "new-password", false, "j", QUIET));
        assertTrue(words, words.startsWith("Joined New Net, but the door sign could not reach the TV Pi on it within 90 seconds"));
    }

    @Test public void refusalsBeforeAnythingChanges() {
        WifiChange c = change();
        assertEquals("Nowhere was not found: look again, or join it as a hidden network", refusal(() -> c.change("Nowhere", "12345678", false, "j", QUIET)));
        assertEquals("Corp asks for a user name: networks like it are not supported", refusal(() -> c.change("Corp", "12345678", false, "j", QUIET)));
        assertEquals("Old uses WEP, which is not safe: networks like it are not supported", refusal(() -> c.change("Old", "12345678", false, "j", QUIET)));
        assertEquals("Guest is an open network: it takes no password", refusal(() -> c.change("Guest", "12345678", false, "j", QUIET)));
        assertEquals("Enter the password for New Net", refusal(() -> c.change("New Net", "", false, "j", QUIET)));
        assertEquals("The door sign is on Office already", refusal(() -> c.change("Office", "", false, "j", QUIET)));
        assertEquals("Modern takes its password, not a key of 64 characters", refusal(() -> c.change("Modern", "a".repeat(64), false, "j", QUIET)));
        assertTrue(wifi.calls.isEmpty());
        assertNull(c.record());
    }

    @Test public void aChangeUnderWayRefusesAnother() throws Exception {
        store.values.put(WifiChange.KEY, new JSONObject().put("state", "trying").put("ssid", "X").toString());
        assertEquals("A Wi-Fi change is under way: wait for it to finish", refusal(() -> change().change("New Net", "new-password", false, "j", QUIET)));
        assertEquals("A Wi-Fi change is under way: wait for it to finish", refusal(() -> change().forget("Office")));
    }

    @Test public void anOpenNetworkAndAHiddenOne() throws Exception {
        wifi.roomOn.add("Guest");
        assertEquals("Joined Guest. The door sign reaches the TV Pi", change().change("Guest", "", false, "j", QUIET));
        assertTrue(wifi.calls.contains("add Guest open"));
        wifi.inAir("Secret", "[RSN-PSK-CCMP][ESS]", "secret-pass", 50);
        wifi.hiding.add("Secret");
        wifi.roomOn.add("Secret");
        assertEquals("Secret was not found: look again, or join it as a hidden network",
                refusal(() -> change().change("Secret", "secret-pass", false, "j2", QUIET)));
        assertEquals("Joined Secret. The door sign reaches the TV Pi", change().change("Secret", "secret-pass", true, "j3", QUIET));
        assertTrue(wifi.calls.contains("add Secret psk hidden"));
    }

    @Test public void aSavedNetworkIsJoinedWithItsSavedPasswordAndKeptIfItFails() throws Exception {
        int guestNet = wifi.save("New Net", "new-password");
        wifi.roomOn.remove("New Net");
        refusal(() -> change().change("New Net", "", false, "j", QUIET));
        assertTrue(wifi.saved.containsKey(guestNet));          // the change did not make it, so it does not remove it
        assertFalse(wifi.calls.stream().anyMatch(call -> call.startsWith("add")));
        assertEquals(office, (int) wifi.on);
    }

    @Test public void aNewPasswordForTheSameNetworkReplacesTheOldOne() throws Exception {
        String words = change().change("Office", "office-pass", false, "j", QUIET);
        assertEquals("Joined Office. The door sign reaches the TV Pi", words);
        assertEquals(1, wifi.saved.size());
        assertFalse(wifi.saved.containsKey(office));
    }

    @Test public void onEthernetOnlyJoiningIsChecked() throws Exception {
        wifi.ethernet = true;
        wifi.roomOn.remove("New Net");
        assertEquals("Joined New Net. The door sign reaches the TV Pi (it is on Ethernet as well, so only joining New Net was checked)",
                change().change("New Net", "new-password", false, "j", QUIET));
    }

    @Test public void withoutANetworkBeforeItGoesOffWifiAgain() {
        wifi.on = null;
        wifi.roomOn.clear();
        assertEquals("Joined New Net, but the door sign could not reach the TV Pi on it within 90 seconds. The door sign is off Wi-Fi again",
                refusal(() -> change().change("New Net", "new-password", false, "j", QUIET)));
    }

    /** A power cut at each step of a change, then the app's next start: the panel ends on the new network only if it was proven. */
    @Test public void aPowerCutAtAnyStepEndsOnTheOldNetworkUnlessTheNewOneWasProven() throws Exception {
        for (boolean works : new boolean[] {true, false}) {
            for (int cut = 1; ; cut++) {
                setUp();
                steps = 0;
                cutAt = -1;
                if (!works) wifi.roomOn.remove("New Net");
                cutAt = cut;
                boolean finished = false;
                try {
                    change().change("New Net", "new-password", false, "j", QUIET);
                    finished = true;
                } catch (PowerCut powerCut) {
                    // the panel went down here
                } catch (Refused refused) {
                    finished = true;
                }
                cutAt = -1;
                // The boot: Android may join any saved network by itself; here the new one when it is saved (the worst case)
                for (Map.Entry<Integer, String[]> n : wifi.saved.entrySet()) if (n.getValue()[0].equals("New Net")) wifi.on = n.getKey();
                JSONObject record = change().recover();
                String state = record == null ? "none" : record.getString("state");
                boolean proven = record != null && record.getString("state").equals("done");
                String where = "works=" + works + " cut=" + cut + " state=" + state;
                if (proven) {
                    assertTrue(where, works);
                    assertEquals(where, "New Net", wifi.current().ssid);
                } else {
                    assertEquals(where, "Office", wifi.current().ssid);
                    assertFalse(where, savedNames().contains("New Net"));
                }
                assertTrue(where, savedNames().contains("Office"));
                assertFalse(where, change().underWay());
                // A second start has nothing left to do
                int calls = wifi.calls.size();
                change().recover();
                assertEquals(where, calls, wifi.calls.size());
                if (finished) break;
            }
        }
    }

    @Test public void forget() throws Exception {
        wifi.save("Guest", "");
        WifiChange c = change();
        assertEquals("The door sign does not know Nowhere", refusal(() -> c.forget("Nowhere")));
        assertEquals("The door sign is on Office: join another network first, or it would be cut off", refusal(() -> c.forget("Office")));
        assertEquals("Forgot Guest", c.forget("Guest"));
        assertFalse(savedNames().contains("Guest"));
        wifi.ethernet = true;
        assertEquals("Forgot Office", c.forget("Office"));
    }

    @Test public void theNetworksInRangeOnePerNameStrongestFirst() throws Exception {
        wifi.inAir("", "[ESS]", null, 99);                           // a hidden network: no name to show
        JSONObject scan = change().scan(true);
        JSONArray networks = scan.getJSONArray("networks");
        assertEquals("New Net", networks.getJSONObject(0).getString("ssid"));
        assertEquals(6, networks.length());
        JSONObject officeSeen = networks.getJSONObject(1);
        assertEquals("Office", officeSeen.getString("ssid"));
        assertTrue(officeSeen.getBoolean("saved"));
        assertTrue(officeSeen.getBoolean("in_use"));
        assertEquals("WPA2", officeSeen.getString("security"));
        assertTrue(networks.getJSONObject(3).getBoolean("open"));       // Guest
        assertEquals("psk", WifiChange.kind("[WPA2-PSK-CCMP][RSN-PSK+SAE-CCMP][ESS]"));
        assertEquals("sae", WifiChange.kind("[RSN-SAE-CCMP][ESS]"));
        assertEquals("enterprise", WifiChange.kind("[WPA2-EAP/SHA1-CCMP][ESS]"));
        assertEquals("owe", WifiChange.kind("[RSN-OWE-CCMP][ESS]"));
        assertEquals("open", WifiChange.kind("[ESS]"));
        assertEquals(100, WifiChange.percent(-40));
        assertEquals(0, WifiChange.percent(-110));
        assertEquals(60, WifiChange.percent(-70));
    }

    @Test public void howThePanelIsConnectedForItsCheckIns() throws Exception {
        JSONObject status = change().status();
        assertEquals("wifi", status.getString("connection"));
        assertEquals("Office", status.getString("ssid"));
        assertEquals(70, status.getInt("signal"));
        assertEquals("10.0.0.20", status.getString("address"));
        assertEquals("Office", status.getJSONArray("saved").getString(0));
        assertTrue(status.isNull("change"));
        wifi.ethernet = true;
        assertEquals("ethernet", change().status().getString("connection"));
        wifi.ethernet = false;
        wifi.roomOn.remove("New Net");
        try {
            change().change("New Net", "new-password", false, "j", QUIET);
        } catch (Refused expected) {
            // gone back
        }
        JSONObject last = change().status().getJSONObject("change");
        assertEquals("undone", last.getString("state"));
        assertEquals("New Net", last.getString("ssid"));
    }
}
