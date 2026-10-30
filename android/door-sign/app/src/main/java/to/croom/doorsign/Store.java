package to.croom.doorsign;

/** A few named strings the app keeps in its private storage (SharedPreferences on the panel; a map in the tests). */
interface Store {
    /** The value kept under `key`, or null. */
    String get(String key);

    /** Keep `value` under `key` (null removes it), written before this returns. */
    void put(String key, String value);
}
