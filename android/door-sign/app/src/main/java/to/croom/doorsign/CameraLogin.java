package to.croom.doorsign;

import java.util.Objects;
import org.json.JSONObject;

/**
 * The camera login the room hands a joined door sign in its check-in answer (spec 2026-11-06): {"user", "password", "name", "sound"}.
 * UniFi Protect adopts the panel's camera with it; `name` is what the camera calls itself ("Conference Room B door sign"); `sound`
 * is the room's Door sign camera sound switch, which adds the microphone (spec 2026-11-10). Kept in the app's own storage, so the
 * camera goes on when the TV Pi is away; an answer without one leaves it as it is.
 */
final class CameraLogin {

    private static final String USER = "camera_user";
    private static final String PASSWORD = "camera_password";
    private static final String NAME = "camera_name";
    private static final String SOUND = "camera_sound";

    final String user;
    final String password;
    final String name;
    final boolean sound;

    CameraLogin(String user, String password, String name) {
        this(user, password, name, false);
    }

    CameraLogin(String user, String password, String name, boolean sound) {
        this.user = user;
        this.password = password;
        this.name = name == null ? "" : name;
        this.sound = sound;
    }

    /** The answer's block, or null unless it has a user and a password. */
    static CameraLogin from(Object json) {
        if (!(json instanceof JSONObject)) return null;
        JSONObject block = (JSONObject) json;
        String user = block.optString("user", "").trim();
        String password = block.optString("password", "");
        if (user.isEmpty() || password.trim().isEmpty()) return null;
        return new CameraLogin(user, password, block.optString("name", "").trim(), Boolean.TRUE.equals(block.opt("sound")));
    }

    void save(Store store) {
        store.put(USER, user);
        store.put(PASSWORD, password);
        store.put(NAME, name);
        store.put(SOUND, sound ? "1" : null);
    }

    /** The login kept, or null when there is none. */
    static CameraLogin load(Store store) {
        String user = store.get(USER);
        String password = store.get(PASSWORD);
        return user == null || password == null ? null : new CameraLogin(user, password, store.get(NAME), "1".equals(store.get(SOUND)));
    }

    @Override public boolean equals(Object other) {
        if (!(other instanceof CameraLogin)) return false;
        CameraLogin that = (CameraLogin) other;
        return user.equals(that.user) && password.equals(that.password) && name.equals(that.name) && sound == that.sound;
    }

    @Override public int hashCode() {
        return Objects.hash(user, password, name, sound);
    }

    @Override public String toString() {
        return "CameraLogin(" + user + ", " + name + (sound ? ", with sound" : "") + ")";     // never the password, in a log
    }
}
