package to.croom.doorsign;

import android.app.AlarmManager;
import android.app.PendingIntent;
import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.os.SystemClock;
import android.util.Log;

/**
 * Brings the sign back when its process ended under it. The app's own update does that (MY_PACKAGE_REPLACED), but so does an update of
 * something it uses (the Play Store's WebView update stops every app with a WebView) or Android ending it, and then the panel shows
 * whatever home screen is left behind the sign: Room B sat on the stock launcher for two days. So Android also wakes this receiver
 * every minute, starting the process if it has to, and a process the sign has not been opened in yet opens it. A sign left by
 * Setup's Exit to Android stays left, until Home or until its process ends.
 */
public class SignWatchdog extends BroadcastReceiver {

    static final long EVERY_MS = 60_000;

    /** From the app's start: the alarm outlives the process (not a reboot, which starts the sign as the home app anyway). */
    static void schedule(Context context) {
        try {
            PendingIntent tick = PendingIntent.getBroadcast(context, 0, new Intent(context, SignWatchdog.class), PendingIntent.FLAG_IMMUTABLE);
            context.getSystemService(AlarmManager.class)
                    .setRepeating(AlarmManager.ELAPSED_REALTIME, SystemClock.elapsedRealtime() + EVERY_MS, EVERY_MS, tick);
        } catch (RuntimeException e) {
            Log.w("DoorSign", "Could not set the sign's watchdog: " + e);
        }
    }

    @Override public void onReceive(Context context, Intent intent) {
        if (MainActivity.created) return;
        Log.i("DoorSign", "The sign was not on: opening it");
        context.startActivity(new Intent(context, MainActivity.class).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK));
    }
}
