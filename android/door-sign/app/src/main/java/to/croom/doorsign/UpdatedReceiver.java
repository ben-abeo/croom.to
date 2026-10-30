package to.croom.doorsign;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;

/** The app was replaced by its update: the sign comes back at once (DoorSignApp's start reports the update to the TV Pi). */
public class UpdatedReceiver extends BroadcastReceiver {

    @Override public void onReceive(Context context, Intent intent) {
        if (!Intent.ACTION_MY_PACKAGE_REPLACED.equals(intent.getAction())) return;
        context.startActivity(new Intent(context, MainActivity.class).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK));
    }
}
