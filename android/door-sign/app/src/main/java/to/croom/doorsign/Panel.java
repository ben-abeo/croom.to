package to.croom.doorsign;

import android.Manifest;
import android.app.PendingIntent;
import android.app.admin.DevicePolicyManager;
import android.content.Context;
import android.content.Intent;
import android.content.pm.PackageInfo;
import android.content.pm.PackageInstaller;
import android.content.pm.PackageManager;
import android.content.pm.Signature;
import android.content.pm.SigningInfo;
import android.os.Build;
import android.util.Log;
import java.io.File;
import java.io.FileInputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.util.HashMap;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.Set;
import java.util.function.Supplier;
import org.json.JSONObject;

/**
 * The panel the door sign runs on: what it says of itself in its check-ins (Agent.Device) and the jobs it does (JobRunner.Abilities).
 * Reboot, Wi-Fi and silent updates need the app to be the panel's device owner (DevicePolicyManager.isDeviceOwnerApp); without it the
 * sign and its light bar work as before, it still checks in, and it tells the TV Pi which jobs it can't do and why.
 */
final class Panel implements Agent.Device, JobRunner.Abilities {

    static final String TAG = "DoorSign";
    static final String NOT_OWNER = "The door sign is not the panel's device owner: see its setup guide";
    static final String OLD_ANDROID = "Wi-Fi from Settings needs Android 11 or newer on the panel";
    static final String OLD_ANDROID_UPDATE = "Updates from Settings need Android 9 or newer on the panel";
    static final long INSTALL_WAIT_MS = 3 * 60_000;

    private final Context context;
    private final WifiChange wifi;
    private final Agent.Http http;
    private final Supplier<String> server;
    private final Supplier<Identity> identity;

    Panel(Context context, WifiChange wifi, Agent.Http http, Supplier<String> server, Supplier<Identity> identity) {
        this.context = context.getApplicationContext();
        this.wifi = wifi;
        this.http = http;
        this.server = server;
        this.identity = identity;
    }

    boolean isOwner() {
        return Kiosk.isOwner(context);
    }

    private DevicePolicyManager dpm() {
        return context.getSystemService(DevicePolicyManager.class);
    }

    /** As device owner: the location permission and location on, which Android asks for before it tells network names. */
    void grantWifiPermissions() {
        if (!isOwner()) return;
        try {
            DevicePolicyManager dpm = dpm();
            String pkg = context.getPackageName();
            dpm.setPermissionGrantState(AdminReceiver.component(context), pkg, Manifest.permission.ACCESS_FINE_LOCATION,
                    DevicePolicyManager.PERMISSION_GRANT_STATE_GRANTED);
            if (Build.VERSION.SDK_INT >= 33) {
                dpm.setPermissionGrantState(AdminReceiver.component(context), pkg, Manifest.permission.NEARBY_WIFI_DEVICES,
                        DevicePolicyManager.PERMISSION_GRANT_STATE_GRANTED);
            }
            if (Build.VERSION.SDK_INT >= 30) dpm.setLocationEnabled(AdminReceiver.component(context), true);
        } catch (RuntimeException e) {
            Log.w(TAG, "Could not grant the Wi-Fi permissions: " + e);
        }
    }

    // --- Agent.Device

    @Override public long versionCode() {
        return installedVersion();
    }

    @Override public String versionName() {
        try {
            String name = context.getPackageManager().getPackageInfo(context.getPackageName(), 0).versionName;
            return name == null ? "" : name;
        } catch (PackageManager.NameNotFoundException e) {
            return "";
        }
    }

    @Override public String name() {
        return Build.MODEL == null ? "" : Build.MODEL;
    }

    @Override public JSONObject network() {
        try {
            return wifi.status();
        } catch (RuntimeException e) {
            return new JSONObject();
        }
    }

    @Override public Map<String, String> cannot() {
        Map<String, String> cannot = new LinkedHashMap<>();
        for (String type : Job.TYPES) {
            String why = cannot(type);
            if (why != null) cannot.put(type, why);
        }
        return cannot;
    }

    // --- JobRunner.Abilities

    @Override public String cannot(String type) {
        if (!isOwner()) return NOT_OWNER;
        if (type.startsWith("wifi_") && Build.VERSION.SDK_INT < 30) return OLD_ANDROID;
        if (type.equals(Job.UPDATE) && Build.VERSION.SDK_INT < 28) return OLD_ANDROID_UPDATE;
        return null;
    }

    @Override public void reboot() throws Refused {
        try {
            dpm().reboot(AdminReceiver.component(context));
        } catch (RuntimeException e) {
            throw new Refused("Android did not reboot the panel: " + e.getMessage());
        }
    }

    @Override public JSONObject scan() throws Refused {
        grantWifiPermissions();
        return wifi.scan(true);
    }

    @Override public String connect(String ssid, String password, boolean hidden, String jobId, JobRunner.Progress progress) throws Refused {
        grantWifiPermissions();
        return wifi.change(ssid, password, hidden, jobId, progress);
    }

    @Override public String forget(String ssid) throws Refused {
        grantWifiPermissions();
        return wifi.forget(ssid);
    }

    @Override public long installedVersion() {
        try {
            return code(context.getPackageManager().getPackageInfo(context.getPackageName(), 0));
        } catch (PackageManager.NameNotFoundException e) {
            return 0;
        }
    }

    @Override public JSONObject wifiOutcome(String jobId) {
        JSONObject record = wifi.record();     // DoorSignApp has recovered the change already (WifiChange.recover) by now
        if (record == null || !jobId.equals(record.optString("job"))) return null;
        return record;
    }

    @SuppressWarnings("deprecation")
    static long code(PackageInfo info) {
        return Build.VERSION.SDK_INT >= 28 ? info.getLongVersionCode() : info.versionCode;
    }

    @android.annotation.TargetApi(28)     // cannot(UPDATE) refuses updates on older Android
    @Override public String update(int versionCode, String sha256, long size, JobRunner.Progress progress) throws Refused {
        long installed = installedVersion();
        if (versionCode <= installed) {
            throw new Refused("The door sign has version " + installed + " already: Android installs only a newer one");
        }
        String base = server.get();
        if (base == null) throw new Refused("The sign has no Crystal Meet server");
        File folder = new File(context.getCacheDir(), "updates");
        File apk = new File(folder, "door-sign-" + versionCode + ".apk");
        try {
            clear(folder);
            if (!folder.mkdirs() && !folder.isDirectory()) throw new Refused("Could not make room for the download");
            Map<String, String> headers = new HashMap<>();
            headers.put(CheckIn.ID_HEADER, identity.get().id);
            headers.put(CheckIn.TOKEN_HEADER, identity.get().token);
            int status;
            try {
                status = http.download(base + "/api/devices/apk/" + versionCode, headers, apk, size);
            } catch (IOException e) {
                throw new Refused("Could not download version " + versionCode + " from the TV Pi: " + e.getMessage());
            }
            if (status != 200) throw new Refused("The TV Pi answered " + status + " for version " + versionCode);
            String problem = ApkCheck.download(apk.length(), ApkCheck.sha256(apk), size, sha256);
            if (problem != null) throw new Refused(problem);

            progress.say("Checking version " + versionCode);
            PackageManager pm = context.getPackageManager();
            PackageInfo own = pm.getPackageInfo(context.getPackageName(), PackageManager.GET_SIGNING_CERTIFICATES);
            PackageInfo info = pm.getPackageArchiveInfo(apk.getPath(), PackageManager.GET_SIGNING_CERTIFICATES);
            problem = ApkCheck.app(context.getPackageName(), own.getLongVersionCode(), signers(own.signingInfo), versionCode,
                    info == null ? null : info.packageName, info == null ? -1 : info.getLongVersionCode(),
                    info == null ? null : signers(info.signingInfo));
            if (problem != null) throw new Refused(problem);

            progress.say("Installing version " + versionCode);
            install(apk, versionCode);
            return "Updated to version " + versionCode;
        } catch (IOException e) {
            throw new Refused("Could not install version " + versionCode + ": " + e.getMessage());
        } catch (PackageManager.NameNotFoundException e) {
            throw new Refused("Android does not know the door sign app: " + e.getMessage());
        } finally {
            clear(folder);
        }
    }

    @android.annotation.TargetApi(28)
    private static Set<String> signers(SigningInfo info) {
        Set<String> digests = new HashSet<>();
        if (info == null) return digests;
        Signature[] signatures = info.getApkContentsSigners();
        if (signatures != null) for (Signature s : signatures) digests.add(ApkCheck.sha256(s.toByteArray()));
        return digests;
    }

    /** One PackageInstaller session for the APK; as device owner Android installs it without asking anyone. Returns once Android says
     * it is installed (if the app is still alive to hear it); Refused with Android's words otherwise. */
    @android.annotation.TargetApi(28)
    private void install(File apk, int versionCode) throws IOException, Refused {
        PackageInstaller installer = context.getPackageManager().getPackageInstaller();
        PackageInstaller.SessionParams params = new PackageInstaller.SessionParams(PackageInstaller.SessionParams.MODE_FULL_INSTALL);
        params.setAppPackageName(context.getPackageName());
        params.setSize(apk.length());
        if (Build.VERSION.SDK_INT >= 31) params.setRequireUserAction(PackageInstaller.SessionParams.USER_ACTION_NOT_REQUIRED);
        int session = installer.createSession(params);
        try (PackageInstaller.Session s = installer.openSession(session)) {
            try (InputStream in = new FileInputStream(apk); OutputStream out = s.openWrite("door-sign.apk", 0, apk.length())) {
                byte[] buffer = new byte[64 * 1024];
                for (int n; (n = in.read(buffer)) > 0; ) out.write(buffer, 0, n);
                s.fsync(out);
            }
            Intent done = new Intent(context, InstallReceiver.class).setAction(InstallReceiver.ACTION);
            int flags = PendingIntent.FLAG_UPDATE_CURRENT | (Build.VERSION.SDK_INT >= 31 ? PendingIntent.FLAG_MUTABLE : 0);
            PendingIntent pending = PendingIntent.getBroadcast(context, session, done, flags);
            Log.i(TAG, "Installing version " + versionCode + " (session " + session + ")");
            s.commit(pending.getIntentSender());
        } catch (IOException | RuntimeException e) {
            installer.abandonSession(session);
            throw e;
        }
        Installs.Result result;
        try {
            result = Installs.await(session, INSTALL_WAIT_MS);
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            throw new Refused("The install of version " + versionCode + " was interrupted");
        }
        if (result == null) throw new Refused("Android did not finish installing version " + versionCode + " within 3 minutes");
        if (!result.ok) throw new Refused(result.words);
    }

    private static void clear(File folder) {
        File[] files = folder.listFiles();
        if (files != null) for (File f : files) //noinspection ResultOfMethodCallIgnored
            f.delete();
    }
}
