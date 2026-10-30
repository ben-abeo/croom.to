package to.croom.doorsign;

import java.nio.charset.StandardCharsets;
import java.util.Arrays;
import java.util.HashSet;
import java.util.Set;
import org.json.JSONObject;

/**
 * A job the TV Pi hands the door sign in the answer to a check-in: {"id", "type", "args"}. The TV Pi checks the arguments; the door
 * sign checks them again here. The jobs: reboot, update {version_code, sha256, size} (an APK release of this app, never a commit:
 * that is a touch Pi's), wifi_scan, wifi_connect {ssid, password, hidden}, wifi_forget {ssid}.
 */
final class Job {

    static final String REBOOT = "reboot";
    static final String UPDATE = "update";
    static final String WIFI_SCAN = "wifi_scan";
    static final String WIFI_CONNECT = "wifi_connect";
    static final String WIFI_FORGET = "wifi_forget";
    static final Set<String> TYPES = new HashSet<>(Arrays.asList(REBOOT, UPDATE, WIFI_SCAN, WIFI_CONNECT, WIFI_FORGET));

    static final int MAX_VERSION_CODE = 2_100_000_000;
    static final long MAX_APK_BYTES = 200L * 1024 * 1024;

    final String id;
    final String type;
    final JSONObject args;

    private Job(String id, String type, JSONObject args) {
        this.id = id;
        this.type = type;
        this.args = args;
    }

    /** The job in an answer, or null when it is not one the door sign can even report on (no id of 16 hex, no type). */
    static Job from(Object raw) {
        if (!(raw instanceof JSONObject)) return null;
        JSONObject job = (JSONObject) raw;
        Object id = job.opt("id");
        Object type = job.opt("type");
        if (!(id instanceof String) || !((String) id).matches("[0-9a-f]{16}") || !(type instanceof String)) return null;
        Object args = job.opt("args");
        return new Job((String) id, (String) type, args instanceof JSONObject ? (JSONObject) args : new JSONObject());
    }

    /** Refused, in words, when the job is not one the door sign does or its arguments are not right. */
    void check() throws Refused {
        if (!TYPES.contains(type)) throw new Refused("The door sign does not know the job " + type);
        switch (type) {
            case UPDATE:
                if (args.has("commit")) throw new Refused("A door sign is updated to a release of the app (a version code), not to a commit");
                versionCode();
                sha256();
                size();
                break;
            case WIFI_CONNECT:
                ssid();
                password();
                hidden();
                break;
            case WIFI_FORGET:
                ssid();
                break;
            default:
                break;
        }
    }

    int versionCode() throws Refused {
        Object code = args.opt("version_code");
        if (!(code instanceof Integer || code instanceof Long)) throw new Refused("The update names no version of the app");
        long value = ((Number) code).longValue();
        if (value <= 0 || value > MAX_VERSION_CODE) throw new Refused("The update names no version of the app");
        return (int) value;
    }

    String sha256() throws Refused {
        Object sha = args.opt("sha256");
        if (!(sha instanceof String) || !((String) sha).matches("[0-9a-f]{64}")) throw new Refused("The update does not say the APK's SHA-256");
        return (String) sha;
    }

    long size() throws Refused {
        Object size = args.opt("size");
        if (!(size instanceof Integer || size instanceof Long)) throw new Refused("The update does not say the APK's size");
        long value = ((Number) size).longValue();
        if (value <= 0 || value > MAX_APK_BYTES) throw new Refused("The update does not say the APK's size");
        return value;
    }

    String ssid() throws Refused {
        Object ssid = args.opt("ssid");
        if (!(ssid instanceof String) || !validSsid((String) ssid)) throw new Refused("Choose a network (a name of 1 to 32 characters)");
        return (String) ssid;
    }

    String password() throws Refused {
        Object password = args.opt("password");
        if (password == null || password == JSONObject.NULL) return "";
        if (!(password instanceof String) || !validPassword((String) password)) {
            throw new Refused("A Wi-Fi password is 8 to 63 letters, digits and symbols, or a key of 64 characters 0 to 9 and A to F");
        }
        return (String) password;
    }

    boolean hidden() throws Refused {
        Object hidden = args.opt("hidden");
        if (hidden == null || hidden == JSONObject.NULL) return false;
        if (!(hidden instanceof Boolean)) throw new Refused("Hidden must be true or false");
        return (Boolean) hidden;
    }

    static boolean validSsid(String ssid) {
        if (ssid == null || ssid.trim().isEmpty()) return false;
        int bytes = ssid.getBytes(StandardCharsets.UTF_8).length;
        if (bytes > 32) return false;
        for (int i = 0; i < ssid.length(); i++) if (ssid.charAt(i) < 32 || ssid.charAt(i) == 127) return false;
        return true;
    }

    static boolean validPassword(String password) {
        return password.isEmpty() || password.matches("[\\x20-\\x7e]{8,63}") || password.matches("[0-9A-Fa-f]{64}");
    }
}
