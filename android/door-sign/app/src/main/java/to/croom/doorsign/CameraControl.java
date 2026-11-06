package to.croom.doorsign;

import android.Manifest;
import android.content.Context;
import android.content.Intent;
import android.content.pm.PackageManager;

/**
 * Starts the camera service when the panel should have it (the app is the device owner, holds the room's camera login, and Android has
 * granted it the camera) and stops it otherwise. Called as the app starts, when the sign comes to the front, after the check-in thread
 * grants the camera, and when the room sends a login: the service itself compares the login with the one it runs, and starts again only
 * for a new one. It only checks here, on whatever thread: the grant (a slow call) is the check-in thread's (Panel.grantPermissions).
 */
final class CameraControl {

    enum Action { START, STOP, NOT_GRANTED }

    private CameraControl() {}

    /** What to do. Never START without the camera granted: Android 14 refuses the service then, and that would end the sign's process. */
    static Action decide(boolean owner, boolean hasLogin, boolean cameraGranted) {
        if (!owner || !hasLogin) return Action.STOP;
        return cameraGranted ? Action.START : Action.NOT_GRANTED;
    }

    static void ensure(Context context) {
        Context app = context.getApplicationContext();
        Intent service = new Intent(app, CameraService.class);
        Action action = decide(Kiosk.isOwner(app), CameraLogin.load(new Prefs(app, "check_in")) != null,
                app.checkSelfPermission(Manifest.permission.CAMERA) == PackageManager.PERMISSION_GRANTED);
        if (action != Action.START) {
            app.stopService(service);
            if (action == Action.NOT_GRANTED) CameraService.failedToStart("Android has not granted the app the camera");
            return;
        }
        try {
            app.startForegroundService(service);
        } catch (RuntimeException e) {
            CameraService.failedToStart("Android would not start it: " + e.getMessage());
        }
    }
}
