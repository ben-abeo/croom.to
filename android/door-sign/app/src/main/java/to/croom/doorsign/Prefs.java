package to.croom.doorsign;

import android.content.Context;
import android.content.SharedPreferences;

/** Store in SharedPreferences, private to the app, each write on disk before it returns (commit). */
final class Prefs implements Store {

    private final SharedPreferences prefs;

    Prefs(Context context, String name) {
        prefs = context.getSharedPreferences(name, Context.MODE_PRIVATE);
    }

    @Override public synchronized String get(String key) {
        return prefs.getString(key, null);
    }

    @Override public synchronized void put(String key, String value) {
        SharedPreferences.Editor edit = prefs.edit();
        if (value == null) edit.remove(key);
        else edit.putString(key, value);
        edit.commit();
    }
}
