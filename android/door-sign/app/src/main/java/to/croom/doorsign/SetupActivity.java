package to.croom.doorsign;

import android.app.Activity;
import android.content.Intent;
import android.content.pm.ResolveInfo;
import android.graphics.Color;
import android.os.Bundle;
import android.provider.Settings;
import android.text.InputType;
import android.view.View;
import android.widget.Button;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;
import java.util.List;

/**
 * Setup, behind the PIN once there is one: the sign's server, the PIN, a test of the light bar and Exit to Android. The first
 * time, before there is a PIN, it opens straight away; it always has Exit to Android, so the panel can never lock anyone out.
 */
public class SetupActivity extends Activity {

    private SignSettings settings;
    private LightBar lights;

    @Override protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        settings = new SignSettings(this);
        lights = new LightBar(new LedDevice());
        if (settings.hasPin()) askForPin();
        else showForm(true);
    }

    private void askForPin() {
        LinearLayout page = page();
        page.addView(heading("Enter the PIN"));
        EditText pin = field("PIN", InputType.TYPE_CLASS_NUMBER | InputType.TYPE_NUMBER_VARIATION_PASSWORD);
        page.addView(pin);
        TextView message = text("");
        Button unlock = button("Unlock", v -> {
            if (settings.checkPin(pin.getText().toString())) {
                showForm(false);
            } else {
                pin.setText("");
                message.setText("Wrong PIN");
            }
        });
        page.addView(unlock);
        page.addView(message);
        pin.requestFocus();
    }

    private void showForm(boolean first) {
        LinearLayout page = page();
        page.addView(heading("Door sign setup"));

        page.addView(text("Crystal Meet server (the room's device and port, for example 10.0.0.5:8080)"));
        EditText address = field("10.0.0.5:8080", InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_URI);
        if (settings.url() != null) address.setText(settings.url());
        page.addView(address);

        page.addView(text(first ? "Choose a PIN for this screen (4 to 8 digits)" : "New PIN (leave blank to keep the current one)"));
        EditText pin = field("PIN", InputType.TYPE_CLASS_NUMBER | InputType.TYPE_NUMBER_VARIATION_PASSWORD);
        page.addView(pin);

        TextView message = text("");
        page.addView(button("Save", v -> {
            String url = SignAddress.normalize(address.getText().toString());
            String newPin = pin.getText().toString();
            if (url == null) {
                message.setText("Enter the server's address, for example 10.0.0.5:8080");
            } else if ((first || !newPin.isEmpty()) && !Pin.valid(newPin)) {
                message.setText("The PIN must be 4 to 8 digits");
            } else {
                settings.save(url, newPin.isEmpty() ? null : newPin);
                finish();
            }
        }));
        page.addView(message);

        page.addView(heading("Test the light bar"));
        LinearLayout row = new LinearLayout(this);
        row.addView(button("Green", v -> lights.show("free")));
        row.addView(button("Yellow", v -> lights.show("soon")));
        row.addView(button("Red", v -> lights.show("occupied")));
        row.addView(button("Off", v -> lights.off()));
        page.addView(row);

        page.addView(heading("Room settings"));
        page.addView(text(DoorSignApp.isOwner()
                ? "This app is the panel's device owner: Settings on the touch Pi can reboot the panel, change its Wi-Fi and update the app."
                : "This app is not the panel's device owner, so Settings on the touch Pi can't reboot the panel, change its Wi-Fi or "
                        + "update the app. The setup guide says how to make it the device owner."));
        String state = DoorSignApp.checkInState();
        if (state != null) page.addView(text(state));
        page.addView(text("Version " + versionName()));

        page.addView(heading("Leave the sign"));
        page.addView(text("Opens the tablet's own home screen. Home brings the sign back."));
        page.addView(button("Exit to Android", v -> exitToAndroid()));
    }

    /** The tablet's own home screen (Launcher3 on the SMT1019), or Android's settings when it has none. */
    private void exitToAndroid() {
        lights.off();
        Kiosk.leave(this);      // the lock-task kiosk, when the app is the device owner: Home brings it back with the sign
        sendBroadcast(new Intent(MainActivity.SYSTEM_BARS_SHOW));   // Android's own screens need the status bar the sign hid
        Intent home = new Intent(Intent.ACTION_MAIN).addCategory(Intent.CATEGORY_HOME);
        List<ResolveInfo> homes = getPackageManager().queryIntentActivities(home, 0);
        Intent target = new Intent(Settings.ACTION_SETTINGS);
        for (ResolveInfo info : homes) {
            String pkg = info.activityInfo.packageName;
            if (!pkg.equals(getPackageName()) && !pkg.equals("com.android.settings")) {
                target = new Intent(home).setClassName(pkg, info.activityInfo.name);
                break;
            }
        }
        startActivity(target.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK));
        finish();
    }

    private String versionName() {
        try {
            android.content.pm.PackageInfo info = getPackageManager().getPackageInfo(getPackageName(), 0);
            return Panel.code(info) + (info.versionName == null ? "" : " (" + info.versionName + ")");
        } catch (android.content.pm.PackageManager.NameNotFoundException e) {
            return "unknown";
        }
    }

    private LinearLayout page() {
        LinearLayout page = new LinearLayout(this);
        page.setOrientation(LinearLayout.VERTICAL);
        page.setPadding(dp(32), dp(24), dp(32), dp(24));
        ScrollView scroll = new ScrollView(this);
        scroll.addView(page);
        setContentView(scroll);
        return page;
    }

    private TextView heading(String words) {
        TextView view = text(words);
        view.setTextSize(24);
        view.setPadding(0, dp(24), 0, dp(8));
        return view;
    }

    private TextView text(String words) {
        TextView view = new TextView(this);
        view.setText(words);
        view.setTextSize(18);
        return view;
    }

    private EditText field(String hint, int inputType) {
        EditText view = new EditText(this);
        view.setSingleLine(true);       // before the input type: setSingleLine puts back a transformation that would show a PIN
        view.setInputType(inputType);
        view.setHint(hint);
        view.setHintTextColor(Color.GRAY);   // an example, never mistaken for an address already filled in
        view.setTextSize(20);
        return view;
    }

    private Button button(String words, View.OnClickListener onClick) {
        Button view = new Button(this);
        view.setText(words);
        view.setTextSize(18);
        view.setOnClickListener(onClick);
        return view;
    }

    private int dp(int value) {
        return Math.round(value * getResources().getDisplayMetrics().density);
    }
}
