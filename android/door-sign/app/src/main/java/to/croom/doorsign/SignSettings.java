package to.croom.doorsign;

import android.content.Context;
import android.content.SharedPreferences;

/** What the setup screen keeps: the sign's address and the PIN that opens setup again. */
final class SignSettings {

    private static final String URL = "url";
    private static final String PIN_SALT = "pin_salt";
    private static final String PIN_HASH = "pin_hash";

    private final SharedPreferences prefs;

    SignSettings(Context context) {
        prefs = context.getSharedPreferences("door_sign", Context.MODE_PRIVATE);
    }

    /** The sign's full address, or null before the first setup. */
    String url() {
        return prefs.getString(URL, null);
    }

    boolean hasPin() {
        return pin() != null;
    }

    boolean checkPin(String pin) {
        return Pin.matches(pin, pin());
    }

    /** Saves the address, and the PIN when one is given (null keeps the one there is). */
    void save(String url, String pin) {
        SharedPreferences.Editor edit = prefs.edit().putString(URL, url);
        if (pin != null) {
            Pin.Stored stored = Pin.store(pin);
            edit.putString(PIN_SALT, stored.salt).putString(PIN_HASH, stored.hash);
        }
        edit.apply();
    }

    private Pin.Stored pin() {
        String salt = prefs.getString(PIN_SALT, null);
        String hash = prefs.getString(PIN_HASH, null);
        return salt == null || hash == null ? null : new Pin.Stored(salt, hash);
    }
}
