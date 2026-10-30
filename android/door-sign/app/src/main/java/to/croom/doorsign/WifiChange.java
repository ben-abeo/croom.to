package to.croom.doorsign;

import java.util.ArrayList;
import java.util.Collections;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.TreeSet;
import java.util.function.LongSupplier;
import java.util.function.Supplier;
import org.json.JSONArray;
import org.json.JSONException;
import org.json.JSONObject;

/**
 * The door sign's Wi-Fi from Settings › Network (room settings spec 2026-10-29, section 7), with the same safety net as the TV Pi and
 * the touch Pi: the network the panel is on is kept, the new one is saved and joined, and within 90 seconds the panel must be on it
 * and reach the TV Pi (GET /api/status); otherwise it goes back to the old network and says why. A change that worked keeps the old
 * network as a remembered one (Forget removes it) and drops the same network's older settings.
 *
 * The change is written down before anything changes (the app's storage, "wifi"): a panel that restarts during the 90 seconds goes
 * back at the app's next start ("The door sign restarted before New Net was proven"), and one that restarts after the change was
 * proven keeps it. The password is never written down.
 *
 * Android's WifiManager does the work (WifiPort: a device owner may add, join and remove networks on Android 14); the tests give a
 * stand-in.
 */
final class WifiChange {

    static final long TRIAL_MS = 90_000;
    static final long CHECK_EVERY_MS = 5_000;
    static final long BACK_WAIT_MS = 30_000;
    static final int MAX_NETWORKS = 30;
    static final int MAX_SAVED = 20;
    static final String KEY = "wifi";
    static final String NAME = "door sign";

    /** The network the panel is on. */
    static final class Current {
        final int networkId;
        final String ssid;
        final Integer signal;

        Current(int networkId, String ssid, Integer signal) {
            this.networkId = networkId;
            this.ssid = ssid;
            this.signal = signal;
        }
    }

    /** A network in range, as Android's scan has it. */
    static final class Seen {
        final String ssid;
        final int signal;
        final String capabilities;

        Seen(String ssid, int signal, String capabilities) {
            this.ssid = ssid;
            this.signal = signal;
            this.capabilities = capabilities == null ? "" : capabilities;
        }
    }

    /** A network Android knows (saved). */
    static final class Saved {
        final int networkId;
        final String ssid;

        Saved(int networkId, String ssid) {
            this.networkId = networkId;
            this.ssid = ssid;
        }
    }

    /** What WifiManager does for the change. */
    interface WifiPort {
        boolean hasWifi();

        /** The Wi-Fi network the panel is on (connected), or null. */
        Current current();

        /** The networks in range (`fresh`: scan now; otherwise what Android found last). */
        List<Seen> scan(boolean fresh) throws Refused;

        /** The saved networks; null when Android does not let the app read them. */
        List<Saved> saved();

        /** Save a network (kind: psk, sae, owe or open); its id, or -1 when Android refused it. */
        int add(String ssid, String password, String kind, boolean hidden);

        /** Join a saved network (the others stay saved). */
        boolean connect(int networkId);

        /** Let Android join a saved network by itself again, without joining it now. */
        void allow(int networkId);

        boolean remove(int networkId);

        /** Why Android gave up on a network ("the password was not accepted"), or null. */
        String rejected(int networkId);

        /** Whether the panel reaches the room's TV Pi (GET <room>/api/status answers). */
        boolean reachesRoom();

        boolean onEthernet();

        /** The panel's address on the network, or null. */
        String address();
    }

    interface Sleeper {
        void sleep(long ms);
    }

    private final WifiPort port;
    private final Store store;
    private final LongSupplier clock;
    private final Sleeper sleeper;
    private final Supplier<String> now;
    private List<Seen> lastScan = Collections.emptyList();

    /**
     * @param clock monotonic milliseconds (the 90 seconds).
     * @param now the wall-clock time as ISO-8601, for the record the page shows.
     */
    WifiChange(WifiPort port, Store store, LongSupplier clock, Sleeper sleeper, Supplier<String> now) {
        this.port = port;
        this.store = store;
        this.clock = clock;
        this.sleeper = sleeper;
        this.now = now;
    }

    // --- networks in range

    /** The kind of a network from its scan capabilities ("[WPA2-PSK-CCMP][RSN-PSK-CCMP][ESS]"). */
    static String kind(String capabilities) {
        String caps = capabilities.toUpperCase(Locale.ROOT);
        if (caps.contains("EAP")) return "enterprise";
        if (caps.contains("WEP")) return "wep";
        if (caps.contains("SAE") && !caps.contains("PSK")) return "sae";
        if (caps.contains("PSK")) return "psk";
        if (caps.contains("OWE")) return "owe";
        return "open";
    }

    static String security(String capabilities) {
        String caps = capabilities.toUpperCase(Locale.ROOT);
        switch (kind(capabilities)) {
            case "enterprise": return "WPA2 802.1X";
            case "wep": return "WEP";
            case "sae": return "WPA3";
            case "psk": return caps.contains("SAE") ? "WPA2 WPA3" : caps.contains("RSN") || caps.contains("WPA2") ? "WPA2" : "WPA1";
            case "owe": return "OWE";
            default: return "";
        }
    }

    /** A signal in dBm as the page shows it, 0 to 100. */
    static int percent(int rssi) {
        return Math.max(0, Math.min(100, 2 * (rssi + 100)));
    }

    /** The networks in range, strongest first, one per name, hidden names left out: {"networks": [{ssid, signal, security, kind, open,
     * saved, in_use}]}. */
    JSONObject scan(boolean fresh) throws Refused {
        if (!port.hasWifi()) throw new Refused("This door sign has no Wi-Fi");
        List<Seen> seen = port.scan(fresh);
        lastScan = seen;
        Map<String, Seen> best = new LinkedHashMap<>();
        for (Seen s : seen) {
            if (s.ssid == null || s.ssid.isEmpty()) continue;
            Seen known = best.get(s.ssid);
            if (known == null || s.signal > known.signal) best.put(s.ssid, s);
        }
        List<Seen> networks = new ArrayList<>(best.values());
        networks.sort((a, b) -> a.signal != b.signal ? b.signal - a.signal : a.ssid.compareToIgnoreCase(b.ssid));
        TreeSet<String> saved = savedNames();
        Current current = port.current();
        JSONArray list = new JSONArray();
        try {
            for (Seen s : networks.subList(0, Math.min(MAX_NETWORKS, networks.size()))) {
                String kind = kind(s.capabilities);
                list.put(new JSONObject().put("ssid", s.ssid).put("signal", Math.max(0, Math.min(100, s.signal)))
                        .put("security", security(s.capabilities)).put("kind", kind).put("open", kind.equals("open") || kind.equals("owe"))
                        .put("saved", saved.contains(s.ssid)).put("in_use", current != null && s.ssid.equals(current.ssid)));
            }
            return new JSONObject().put("networks", list).put("at", now.get());
        } catch (JSONException e) {
            throw new IllegalStateException(e);
        }
    }

    private List<Saved> saved() {
        List<Saved> saved = port.saved();
        return saved == null ? Collections.emptyList() : saved;
    }

    /** The saved networks, for a change: one made without knowing them could save over one and lose it. */
    private List<Saved> savedOrRefuse() throws Refused {
        List<Saved> saved = port.saved();
        if (saved == null) throw new Refused("Android did not let the door sign read its saved networks: nothing was changed");
        return saved;
    }

    private TreeSet<String> savedNames() {
        TreeSet<String> names = new TreeSet<>(String.CASE_INSENSITIVE_ORDER);
        for (Saved s : saved()) if (s.ssid != null && !s.ssid.isEmpty()) names.add(s.ssid);
        return names;
    }

    private List<Saved> savedAs(String ssid) {
        List<Saved> found = new ArrayList<>();
        for (Saved s : saved()) if (ssid.equals(s.ssid)) found.add(s);
        return found;
    }

    // --- how the panel is connected, for its check-ins

    JSONObject status() {
        JSONObject status = new JSONObject();
        try {
            boolean wifi = port.hasWifi();
            Current current = wifi ? port.current() : null;
            String connection = port.onEthernet() ? "ethernet" : current != null ? "wifi" : "none";
            status.put("address", port.address() == null ? JSONObject.NULL : port.address());
            status.put("wifi", wifi);
            status.put("connection", connection);
            status.put("ssid", current == null || current.ssid == null ? JSONObject.NULL : current.ssid);
            status.put("signal", current == null || current.signal == null ? JSONObject.NULL : current.signal);
            JSONArray saved = new JSONArray();
            if (wifi) for (String name : savedNames()) if (saved.length() < MAX_SAVED) saved.put(name);
            status.put("saved", saved);
            JSONObject change = publicRecord();
            status.put("change", change == null ? JSONObject.NULL : change);
        } catch (JSONException e) {
            throw new IllegalStateException(e);
        }
        return status;
    }

    // --- the record

    JSONObject record() {
        String text = store.get(KEY);
        if (text == null) return null;
        try {
            return new JSONObject(text);
        } catch (JSONException e) {
            return null;
        }
    }

    /** The last change as the page shows it: {"state", "ssid", "words", "at"}; null before any. */
    JSONObject publicRecord() {
        JSONObject record = record();
        if (record == null) return null;
        try {
            return new JSONObject().put("state", record.optString("state")).put("ssid", record.optString("ssid"))
                    .put("words", record.optString("words")).put("at", record.optString("at"));
        } catch (JSONException e) {
            throw new IllegalStateException(e);
        }
    }

    boolean underWay() {
        JSONObject record = record();
        if (record == null) return false;
        String state = record.optString("state");
        return state.equals("trying") || state.equals("proven") || state.equals("undoing");
    }

    private void save(JSONObject record, String state, String words) {
        try {
            if (state != null) record.put("state", state);
            if (words != null) record.put("words", words);
            record.put("at", now.get());
        } catch (JSONException e) {
            throw new IllegalStateException(e);
        }
        store.put(KEY, record.toString());
    }

    // --- a change

    /** Join `ssid` (with `password`, or the saved one when it is empty) and keep it only if the panel reaches the TV Pi on it within 90
     * seconds. The words of what happened; Refused with them when it went back or never began. */
    String change(String ssid, String password, boolean hidden, String jobId, JobRunner.Progress progress) throws Refused {
        if (!Job.validSsid(ssid)) throw new Refused("Choose a network (a name of 1 to 32 characters)");
        if (password == null) password = "";
        if (!Job.validPassword(password)) {
            throw new Refused("A Wi-Fi password is 8 to 63 letters, digits and symbols, or a key of 64 characters 0 to 9 and A to F");
        }
        if (underWay()) throw new Refused("A Wi-Fi change is under way: wait for it to finish");
        if (!port.hasWifi()) throw new Refused("This door sign has no Wi-Fi");
        Seen seen = null;
        for (Seen s : lastScan.isEmpty() ? port.scan(false) : lastScan) {
            if (ssid.equals(s.ssid) && (seen == null || s.signal > seen.signal)) seen = s;
        }
        String kind;
        if (seen != null) kind = kind(seen.capabilities);
        else if (hidden) kind = password.isEmpty() ? "open" : "psk";
        else throw new Refused(ssid + " was not found: look again, or join it as a hidden network");
        if (kind.equals("enterprise")) throw new Refused(ssid + " asks for a user name: networks like it are not supported");
        if (kind.equals("wep")) throw new Refused(ssid + " uses WEP, which is not safe: networks like it are not supported");
        if ((kind.equals("open") || kind.equals("owe")) && !password.isEmpty()) throw new Refused(ssid + " is an open network: it takes no password");
        if (kind.equals("sae") && password.length() == 64) throw new Refused(ssid + " takes its password, not a key of 64 characters");
        List<Saved> all = savedOrRefuse();
        List<Saved> profiles = savedAs(ssid);
        Current old = port.current();
        if (old != null && old.ssid == null) {
            throw new Refused("Android did not let the door sign read the network it is on: nothing was changed");
        }
        if (old != null && ssid.equals(old.ssid)) {
            // Android saves a network's new password over the one it is on (the same name and security): a wrong one would cut it off
            if (password.isEmpty()) throw new Refused("The door sign is on " + ssid + " already");
            throw new Refused("This door sign is on " + ssid + " now: join another network first");
        }
        if (!profiles.isEmpty() && !password.isEmpty()) {
            // Likewise for a saved network: its password can't be read back, so a change that fails could not put it back
            throw new Refused("Forget " + ssid + " first, then join it again");
        }
        int reuse = !profiles.isEmpty() ? profiles.get(0).networkId : -1;
        if ((kind.equals("psk") || kind.equals("sae")) && password.isEmpty() && reuse < 0) throw new Refused("Enter the password for " + ssid);

        JSONObject record = new JSONObject();
        try {
            JSONArray before = new JSONArray();
            for (Saved s : profiles) before.put(s.networkId);
            JSONArray known = new JSONArray();          // every network saved before the change: none of them is ever removed by it
            for (Saved s : all) known.put(s.networkId);
            record.put("known", known);
            record.put("ssid", ssid).put("old", old == null ? -1 : old.networkId).put("old_name", old == null ? "" : old.ssid)
                    .put("new", reuse).put("created", reuse < 0).put("before", before).put("job", jobId == null ? "" : jobId)
                    .put("ethernet", port.onEthernet());
        } catch (JSONException e) {
            throw new IllegalStateException(e);
        }
        save(record, "trying", "Joining " + ssid);          // written before anything changes
        progress.say("Joining " + ssid);
        int id = reuse;
        if (id < 0) {
            id = port.add(ssid, password, kind, hidden);
            if (id < 0) throw new Refused(goBack(record, "Could not save " + ssid + "'s settings: Android refused them"));
            put(record, "new", id);
            if (ids(record, "known").contains(id) || (old != null && old.networkId == id)) {
                // Android saved them over a network it knew (the same name and security, which the refusals above should have
                // caught): its password can't be read back to put back, so it is never removed, and the words say so
                put(record, "created", false);
                put(record, "overwrote", true);
            }
            save(record, null, null);
        }
        if (!port.connect(id)) throw new Refused(goBack(record, "Could not join " + ssid + ": Android refused to"));
        save(record, null, "Joining " + ssid + ": checking that the door sign reaches the TV Pi on it");
        progress.say("Joining " + ssid + ": checking that the door sign reaches the TV Pi on it");

        long deadline = clock.getAsLong() + TRIAL_MS;
        boolean everOn = false;
        boolean proven = false;
        while (true) {
            String why = port.rejected(id);
            if (why != null) throw new Refused(goBack(record, "Could not join " + ssid + ": " + why));
            boolean on = onNetwork(id);
            everOn |= on;
            if (on && port.reachesRoom() && onNetwork(id)) {
                proven = true;
                break;
            }
            if (clock.getAsLong() >= deadline) break;
            sleeper.sleep(CHECK_EVERY_MS);
        }
        if (!proven) {
            throw new Refused(goBack(record, everOn
                    ? "Joined " + ssid + ", but the door sign could not reach the TV Pi on it within 90 seconds"
                    : "Could not join " + ssid + " within 90 seconds"));
        }
        save(record, "proven", "Joined " + ssid);
        return keep(record);
    }

    private static List<Integer> ids(JSONObject record, String key) {
        List<Integer> ids = new ArrayList<>();
        JSONArray kept = record.optJSONArray(key);
        for (int i = 0; kept != null && i < kept.length(); i++) ids.add(kept.optInt(i));
        return ids;
    }

    private boolean onNetwork(int networkId) {
        Current current = port.current();
        return current != null && current.networkId == networkId;
    }

    private static void put(JSONObject record, String key, Object value) {
        try {
            record.put(key, value);
        } catch (JSONException e) {
            throw new IllegalStateException(e);
        }
    }

    /** The change worked: Android may join the old network by itself again (it stays remembered), the same network's older settings
     * go. The words. */
    private String keep(JSONObject record) {
        String ssid = record.optString("ssid");
        int id = record.optInt("new", -1);
        int old = record.optInt("old", -1);
        if (old >= 0 && old != id) port.allow(old);
        if (record.optBoolean("created")) {
            for (Saved s : savedAs(ssid)) if (s.networkId != id && s.networkId != old) port.remove(s.networkId);
        }
        String words = "Joined " + ssid + ". The door sign reaches the TV Pi"
                + (record.optBoolean("ethernet") ? " (it is on Ethernet as well, so only joining " + ssid + " was checked)" : "");
        save(record, "done", words);
        return words;
    }

    /** Back to the network the panel was on (none: off Wi-Fi again), the new network's settings removed when the change made them. The
     * words. */
    String goBack(JSONObject record, String why) {
        save(record, "undoing", why + ": going back");
        String ssid = record.optString("ssid");
        int id = record.optInt("new", -1);
        int old = record.optInt("old", -1);
        String oldName = record.optString("old_name");
        if (record.optBoolean("created", true)) {
            // The new network's settings, also when Android saved them but the panel restarted before their id was written down; never
            // a network saved before the change, nor the one to go back to
            List<Integer> keep = ids(record, record.has("known") ? "known" : "before");
            for (Saved s : savedAs(ssid)) {
                if (s.networkId != old && !keep.contains(s.networkId) && (s.networkId == id || !ids(record, "before").contains(s.networkId))) {
                    port.remove(s.networkId);
                }
            }
        }
        if (record.optBoolean("overwrote")) {
            why = why + ". Android saved the new password over " + ssid + "'s, which can't be put back: forget " + ssid
                    + " and join it again with its password";
        }
        String words;
        if (old < 0) {
            words = why + ". The door sign is off Wi-Fi again";
        } else {
            boolean back = port.connect(old);
            long until = clock.getAsLong() + BACK_WAIT_MS;
            while (back && !onNetwork(old) && clock.getAsLong() < until) sleeper.sleep(2_000);
            if (back && onNetwork(old)) words = why + ". The door sign is back on " + oldName;
            else words = why + ", and going back to " + oldName + " has not worked yet: Android keeps trying it";
        }
        save(record, "undone", words);
        return words;
    }

    /**
     * At the app's start (after a power cut, a crash, a reboot): a change that was proven is kept; one that was not is undone. The
     * record ({state: done | undone, words, job}) when there was one to report on, else null.
     */
    JSONObject recover() {
        JSONObject record = record();
        if (record == null) return null;
        String state = record.optString("state");
        String ssid = record.optString("ssid");
        if (state.equals("trying") || state.equals("undoing")) {
            goBack(record, "The door sign restarted before " + ssid + " was proven");
        } else if (state.equals("proven")) {
            int id = record.optInt("new", -1);
            if (!onNetwork(id)) {
                port.connect(id);
                long until = clock.getAsLong() + BACK_WAIT_MS;
                while (!onNetwork(id) && clock.getAsLong() < until) sleeper.sleep(2_000);
            }
            if (onNetwork(id)) keep(record);
            else goBack(record, "The door sign did not stay on " + ssid + " after it restarted");
        }
        return record();
    }

    /** Forget a network the panel knows (each of its saved settings). The words; Refused when it can't. */
    String forget(String ssid) throws Refused {
        if (!Job.validSsid(ssid)) throw new Refused("Choose a network (a name of 1 to 32 characters)");
        if (underWay()) throw new Refused("A Wi-Fi change is under way: wait for it to finish");
        savedOrRefuse();
        List<Saved> profiles = savedAs(ssid);
        if (profiles.isEmpty()) throw new Refused("The door sign does not know " + ssid);
        Current current = port.current();
        if (current != null && ssid.equals(current.ssid) && !port.onEthernet()) {
            throw new Refused("The door sign is on " + ssid + ": join another network first, or it would be cut off");
        }
        for (Saved s : profiles) {
            if (!port.remove(s.networkId)) throw new Refused("Could not forget " + ssid + ": Android refused to");
        }
        return "Forgot " + ssid;
    }
}
