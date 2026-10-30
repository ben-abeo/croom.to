package to.croom.doorsign;

import java.util.HashMap;
import java.util.Map;

/**
 * The installs under way: Android tells the result of a PackageInstaller session to InstallReceiver, which hands it here to the job
 * that waits for it. When the install of an update takes, Android ends the app first, and the next start reports it instead.
 */
final class Installs {

    static final class Result {
        final boolean ok;
        final String words;

        Result(boolean ok, String words) {
            this.ok = ok;
            this.words = words;
        }
    }

    private static final Map<Integer, Result> results = new HashMap<>();

    private Installs() {}

    static synchronized void finished(int session, boolean ok, String words) {
        results.put(session, new Result(ok, words));
        Installs.class.notifyAll();
    }

    /** The result of `session`, or null when there is none within `ms`. */
    static synchronized Result await(int session, long ms) throws InterruptedException {
        long until = System.currentTimeMillis() + ms;
        while (!results.containsKey(session)) {
            long left = until - System.currentTimeMillis();
            if (left <= 0) return null;
            Installs.class.wait(left);
        }
        return results.remove(session);
    }
}
