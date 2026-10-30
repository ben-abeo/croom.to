package to.croom.doorsign;

import android.annotation.SuppressLint;
import android.content.Context;
import android.content.pm.PackageManager;
import android.net.ConnectivityManager;
import android.net.LinkAddress;
import android.net.LinkProperties;
import android.net.Network;
import android.net.NetworkCapabilities;
import android.net.wifi.ScanResult;
import android.net.wifi.SupplicantState;
import android.net.wifi.WifiConfiguration;
import android.net.wifi.WifiInfo;
import android.net.wifi.WifiManager;
import android.os.Build;
import android.os.SystemClock;
import java.net.Inet4Address;
import java.util.ArrayList;
import java.util.List;
import java.util.function.Supplier;

/**
 * WifiChange's work done by Android's WifiManager. On Android 10 and newer only a device owner (or the system) may add, join and
 * remove networks with these calls, and read the network names (with the location permission, which the device owner grants itself,
 * and location on): the app checks it is the device owner before any Wi-Fi job (Panel.cannot).
 */
@SuppressWarnings("deprecation")
@SuppressLint("MissingPermission")
final class AndroidWifi implements WifiChange.WifiPort {

    private final Context context;
    private final WifiManager wifi;
    private final ConnectivityManager connectivity;
    private final Supplier<String> server;
    private final Agent.Http http;

    /** @param server the room's address ("http://10.0.0.5:8080"), whose /api/status the panel must reach on a new network. */
    AndroidWifi(Context context, Supplier<String> server, Agent.Http http) {
        this.context = context.getApplicationContext();
        this.wifi = this.context.getSystemService(WifiManager.class);
        this.connectivity = this.context.getSystemService(ConnectivityManager.class);
        this.server = server;
        this.http = http;
    }

    static String unquote(String ssid) {
        if (ssid == null || ssid.equals(WifiManager.UNKNOWN_SSID)) return null;
        if (ssid.length() >= 2 && ssid.startsWith("\"") && ssid.endsWith("\"")) return ssid.substring(1, ssid.length() - 1);
        return ssid;
    }

    @Override public boolean hasWifi() {
        return wifi != null && context.getPackageManager().hasSystemFeature(PackageManager.FEATURE_WIFI);
    }

    @Override public WifiChange.Current current() {
        if (wifi == null) return null;
        WifiInfo info = wifi.getConnectionInfo();
        if (info == null || info.getNetworkId() == -1 || info.getSupplicantState() != SupplicantState.COMPLETED) return null;
        return new WifiChange.Current(info.getNetworkId(), unquote(info.getSSID()), WifiChange.percent(info.getRssi()));
    }

    @Override public List<WifiChange.Seen> scan(boolean fresh) throws Refused {
        if (wifi == null) throw new Refused("This door sign has no Wi-Fi");
        if (!wifi.isWifiEnabled()) wifi.setWifiEnabled(true);
        if (fresh && wifi.startScan()) SystemClock.sleep(4_000);   // Android may refuse a scan this soon after another: then what it found last
        List<WifiChange.Seen> seen = new ArrayList<>();
        try {
            for (ScanResult result : wifi.getScanResults()) {
                String ssid = Build.VERSION.SDK_INT >= 33 && result.getWifiSsid() != null ? unquote(result.getWifiSsid().toString()) : result.SSID;
                seen.add(new WifiChange.Seen(ssid, WifiChange.percent(result.level), result.capabilities));
            }
        } catch (SecurityException e) {
            throw new Refused("Android did not let the door sign look for networks: " + e.getMessage());
        }
        return seen;
    }

    @Override public List<WifiChange.Saved> saved() {
        List<WifiChange.Saved> saved = new ArrayList<>();
        if (wifi == null) return saved;
        try {
            List<WifiConfiguration> configured = wifi.getConfiguredNetworks();
            if (configured == null) return null;
            for (WifiConfiguration c : configured) saved.add(new WifiChange.Saved(c.networkId, unquote(c.SSID)));
        } catch (SecurityException e) {
            return null;    // not the device owner, or no location permission: WifiChange changes nothing without them
        }
        return saved;
    }

    @Override public int add(String ssid, String password, String kind, boolean hidden) {
        if (Build.VERSION.SDK_INT < 30) return -1;
        if (!wifi.isWifiEnabled()) wifi.setWifiEnabled(true);
        WifiConfiguration config = new WifiConfiguration();
        config.SSID = "\"" + ssid + "\"";
        config.hiddenSSID = hidden;
        switch (kind) {
            case "sae":
                config.setSecurityParams(WifiConfiguration.SECURITY_TYPE_SAE);
                config.preSharedKey = "\"" + password + "\"";
                break;
            case "psk":
                config.setSecurityParams(WifiConfiguration.SECURITY_TYPE_PSK);
                config.preSharedKey = password.length() == 64 ? password : "\"" + password + "\"";
                break;
            case "owe":
                config.setSecurityParams(WifiConfiguration.SECURITY_TYPE_OWE);
                break;
            default:
                config.setSecurityParams(WifiConfiguration.SECURITY_TYPE_OPEN);
        }
        return wifi.addNetwork(config);
    }

    @Override public boolean connect(int networkId) {
        if (!wifi.isWifiEnabled()) wifi.setWifiEnabled(true);
        return wifi.enableNetwork(networkId, true);
    }

    @Override public void allow(int networkId) {
        wifi.enableNetwork(networkId, false);
    }

    @Override public boolean remove(int networkId) {
        return wifi.removeNetwork(networkId);
    }

    @Override public String rejected(int networkId) {
        // Android tells why it gave up on a network (a wrong password) only to system apps: the 90 seconds run out instead
        return null;
    }

    @Override public boolean reachesRoom() {
        String base = server.get();
        return base != null && http.answers(base + "/api/status");
    }

    @Override public boolean onEthernet() {
        if (connectivity == null) return false;
        for (Network network : connectivity.getAllNetworks()) {
            NetworkCapabilities caps = connectivity.getNetworkCapabilities(network);
            if (caps != null && caps.hasTransport(NetworkCapabilities.TRANSPORT_ETHERNET)
                    && caps.hasCapability(NetworkCapabilities.NET_CAPABILITY_INTERNET)) return true;
        }
        return false;
    }

    @Override public String address() {
        if (connectivity == null) return null;
        Network active = connectivity.getActiveNetwork();
        LinkProperties link = active == null ? null : connectivity.getLinkProperties(active);
        if (link == null) return null;
        for (LinkAddress address : link.getLinkAddresses()) {
            if (address.getAddress() instanceof Inet4Address) return address.getAddress().getHostAddress();
        }
        return null;
    }
}
