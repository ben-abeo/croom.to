package to.croom.doorsign;

import android.app.admin.DeviceAdminReceiver;
import android.content.ComponentName;
import android.content.Context;

/**
 * The door sign app as the panel's device owner (set once per panel, after a factory reset:
 * adb shell dpm set-device-owner to.croom.doorsign/.AdminReceiver). As device owner it reboots the panel, changes its Wi-Fi and
 * installs its own updates without a person confirming, and keeps the sign in Android's lock-task kiosk.
 */
public class AdminReceiver extends DeviceAdminReceiver {

    static ComponentName component(Context context) {
        return new ComponentName(context, AdminReceiver.class);
    }
}
