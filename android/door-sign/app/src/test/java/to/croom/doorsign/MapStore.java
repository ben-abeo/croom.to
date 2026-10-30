package to.croom.doorsign;

import java.util.HashMap;
import java.util.Map;

/** Store in a map, for the tests: `writes` counts the writes. */
class MapStore implements Store {
    final Map<String, String> values = new HashMap<>();
    int writes;

    @Override public String get(String key) {
        return values.get(key);
    }

    @Override public void put(String key, String value) {
        writes++;
        if (value == null) values.remove(key);
        else values.put(key, value);
    }
}
