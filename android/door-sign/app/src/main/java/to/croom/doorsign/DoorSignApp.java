package to.croom.doorsign;

import android.app.Application;
import android.os.SystemClock;
import android.util.Log;
import java.security.SecureRandom;
import java.time.Instant;
import java.time.temporal.ChronoUnit;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

/**
 * The app's process: it starts the check-in with the room's TV Pi (Agent) on a thread of its own as soon as the app does (at power-on,
 * the panel starts its home app, the sign), and keeps it going as long as the process lives. A Wi-Fi change or a job the app did not
 * live through is put right, and reported, before the first check-in.
 */
public class DoorSignApp extends Application {

    private static final String TAG = "DoorSign";
    private static volatile Agent agent;
    private static volatile Panel panel;

    @Override public void onCreate() {
        super.onCreate();
        start();
    }

    private synchronized void start() {
        if (agent != null) return;
        SignSettings settings = new SignSettings(this);
        Store store = new Prefs(this, "check_in");
        Agent.Http http = new UrlHttp();
        Identity identity = Identity.load(store, new SecureRandom());
        java.util.function.Supplier<String> server = () -> SignAddress.server(settings.url());
        WifiChange wifi = new WifiChange(new AndroidWifi(this, server, http), store, SystemClock::elapsedRealtime, SystemClock::sleep,
                () -> Instant.now().truncatedTo(ChronoUnit.SECONDS).toString());
        panel = new Panel(this, wifi, http, server, () -> identity);
        ExecutorService jobs = Executors.newSingleThreadExecutor(r -> new Thread(r, "door-sign-job"));
        JobRunner runner = new JobRunner(store, panel, jobs, SystemClock::elapsedRealtime);
        agent = new Agent(server, identity, http, panel, runner, words -> Log.i(TAG, words));
        Agent checkIn = agent;
        Thread thread = new Thread(() -> {
            panel.grantWifiPermissions();
            try {
                if (panel.isOwner()) wifi.recover();     // a Wi-Fi change the panel did not live through: kept if proven, undone if not
            } catch (RuntimeException e) {
                Log.w(TAG, "Could not take up the Wi-Fi change that was under way: " + e);
            }
            runner.resume();
            while (true) {
                long wait;
                try {
                    wait = checkIn.once();
                } catch (RuntimeException e) {
                    Log.w(TAG, "Check-in failed: " + e);
                    wait = Agent.RETRY_MS;
                }
                SystemClock.sleep(wait);
            }
        }, "door-sign-check-in");
        thread.setDaemon(true);
        thread.start();
    }

    /** Where the check-in is, in words (setup shows it), or null before it started. */
    static String checkInState() {
        Agent current = agent;
        return current == null ? null : current.state();
    }

    static boolean isOwner() {
        Panel current = panel;
        return current != null && current.isOwner();
    }
}
