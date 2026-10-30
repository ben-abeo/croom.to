package to.croom.doorsign;

import android.app.Activity;
import android.app.ActivityManager;
import android.app.admin.DevicePolicyManager;
import android.content.Context;
import android.util.Log;

/**
 * Android's lock-task kiosk, when the app is the panel's device owner: the sign is the only thing on the screen (no status bar, no Home
 * or Recents to leave it by). Setup's Exit to Android ends it; Home brings the sign, and the kiosk, back. Without device owner the sign
 * is the home app as before.
 */
final class Kiosk {

    private Kiosk() {}

    static boolean isOwner(Context context) {
        DevicePolicyManager dpm = context.getSystemService(DevicePolicyManager.class);
        return dpm != null && dpm.isDeviceOwnerApp(context.getPackageName());
    }

    static void enter(Activity activity) {
        if (!isOwner(activity)) return;
        try {
            DevicePolicyManager dpm = activity.getSystemService(DevicePolicyManager.class);
            dpm.setLockTaskPackages(AdminReceiver.component(activity), new String[] {activity.getPackageName()});
            ActivityManager am = activity.getSystemService(ActivityManager.class);
            if (am.getLockTaskModeState() == ActivityManager.LOCK_TASK_MODE_NONE) activity.startLockTask();
        } catch (RuntimeException e) {
            Log.w("DoorSign", "Could not start the lock-task kiosk: " + e);
        }
    }

    static void leave(Activity activity) {
        try {
            ActivityManager am = activity.getSystemService(ActivityManager.class);
            if (am.getLockTaskModeState() != ActivityManager.LOCK_TASK_MODE_NONE) activity.stopLockTask();
        } catch (RuntimeException e) {
            Log.w("DoorSign", "Could not leave the lock-task kiosk: " + e);
        }
    }
}
