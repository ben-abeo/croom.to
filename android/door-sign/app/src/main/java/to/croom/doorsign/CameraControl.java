package to.croom.doorsign;

import android.Manifest;
import android.app.admin.DevicePolicyManager;
import android.content.Context;
import android.content.Intent;

/**
 * Starts the camera service when the panel should have it (the app is the device owner and holds the room's camera login) and stops it
 * otherwise. Called as the app starts, when the sign comes to the front, and when the room sends a login: the service itself compares
 * the login with the one it runs, and starts again only for a new one.
 */
final class CameraControl {

    private CameraControl() {}

    static void ensure(Context context) {
        Context app = context.getApplicationContext();
        Intent service = new Intent(app, CameraService.class);
        if (!Kiosk.isOwner(app) || CameraLogin.load(new Prefs(app, "check_in")) == null) {
            app.stopService(service);
            return;
        }
        try {
            app.getSystemService(DevicePolicyManager.class).setPermissionGrantState(AdminReceiver.component(app), app.getPackageName(),
                    Manifest.permission.CAMERA, DevicePolicyManager.PERMISSION_GRANT_STATE_GRANTED);
            app.startForegroundService(service);
        } catch (RuntimeException e) {
            CameraService.failedToStart("Android would not start it: " + e.getMessage());
        }
    }
}
