package to.croom.doorsign;

import java.util.Objects;
import org.json.JSONObject;

/**
 * The camera login the room hands a joined door sign in its check-in answer (spec 2026-11-06): {"user", "password", "name"}. UniFi
 * Protect adopts the panel's camera with it; `name` is what the camera calls itself ("Conference Room B door sign"). Kept in the app's
 * own storage, so the camera goes on when the TV Pi is away; an answer without one leaves it as it is.
 */
final class CameraLogin {

    private static final String USER = "camera_user";
    private static final String PASSWORD = "camera_password";
    private static final String NAME = "camera_name";

    final String user;
    final String password;
    final String name;

    CameraLogin(String user, String password, String name) {
        this.user = user;
        this.password = password;
        this.name = name == null ? "" : name;
    }

    /** The answer's block, or null unless it has a user and a password. */
    static CameraLogin from(Object json) {
        if (!(json instanceof JSONObject)) return null;
        JSONObject block = (JSONObject) json;
        String user = block.optString("user", "").trim();
        String password = block.optString("password", "");
        if (user.isEmpty() || password.trim().isEmpty()) return null;
        return new CameraLogin(user, password, block.optString("name", "").trim());
    }

    void save(Store store) {
        store.put(USER, user);
        store.put(PASSWORD, password);
        store.put(NAME, name);
    }

    /** The login kept, or null when there is none. */
    static CameraLogin load(Store store) {
        String user = store.get(USER);
        String password = store.get(PASSWORD);
        return user == null || password == null ? null : new CameraLogin(user, password, store.get(NAME));
    }

    @Override public boolean equals(Object other) {
        if (!(other instanceof CameraLogin)) return false;
        CameraLogin that = (CameraLogin) other;
        return user.equals(that.user) && password.equals(that.password) && name.equals(that.name);
    }

    @Override public int hashCode() {
        return Objects.hash(user, password, name);
    }

    @Override public String toString() {
        return "CameraLogin(" + user + ", " + name + ")";     // never the password, in a log
    }
}
