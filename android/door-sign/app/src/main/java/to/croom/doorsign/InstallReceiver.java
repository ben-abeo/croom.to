package to.croom.doorsign;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.content.pm.PackageInstaller;
import android.util.Log;

/** Android's word on an update's install session (PackageInstaller), handed to the job that waits for it. */
public class InstallReceiver extends BroadcastReceiver {

    static final String ACTION = "to.croom.doorsign.INSTALLED";

    @Override public void onReceive(Context context, Intent intent) {
        int session = intent.getIntExtra(PackageInstaller.EXTRA_SESSION_ID, -1);
        int status = intent.getIntExtra(PackageInstaller.EXTRA_STATUS, PackageInstaller.STATUS_FAILURE);
        String message = intent.getStringExtra(PackageInstaller.EXTRA_STATUS_MESSAGE);
        Log.i("DoorSign", "Install session " + session + ": status " + status + " " + message);
        if (status == PackageInstaller.STATUS_SUCCESS) {
            Installs.finished(session, true, "Installed");
        } else if (status == PackageInstaller.STATUS_PENDING_USER_ACTION) {
            // Only an app that is not the device owner is asked this: nobody is at the panel to confirm, so the update is given up
            Installs.finished(session, false, "Android asked for a person to confirm the install: the door sign app is not the panel's device owner");
        } else {
            Installs.finished(session, false, "Android did not install it" + (message == null ? "" : ": " + message));
        }
    }
}
