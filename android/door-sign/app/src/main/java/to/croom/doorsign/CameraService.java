package to.croom.doorsign;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.content.pm.ServiceInfo;
import android.net.ConnectivityManager;
import android.net.LinkAddress;
import android.net.LinkProperties;
import android.net.Network;
import android.net.wifi.WifiManager;
import android.os.Build;
import android.os.Handler;
import android.os.IBinder;
import android.os.Looper;
import android.util.Log;
import java.io.FilterOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.DatagramPacket;
import java.net.Inet4Address;
import java.net.InetAddress;
import java.net.MulticastSocket;
import java.net.NetworkInterface;
import java.net.ServerSocket;
import java.net.Socket;
import java.nio.charset.StandardCharsets;
import java.security.SecureRandom;
import java.time.Instant;
import java.util.List;
import java.util.UUID;

/**
 * The door sign's camera for UniFi Protect (spec 2026-11-06), a foreground service of the camera type so it runs whatever the screen
 * shows: the camera and its encoder (CameraStream), the ONVIF answers on TCP 8000, the stream on RTSP 8554 and WS-Discovery on UDP 3702.
 * It runs only while the app is the device owner and holds the room's camera login (CameraControl starts and stops it); without the
 * login nothing listens. Anything that fails stops the lot, says why on the door sign's card, and is tried again in 10 seconds.
 */
public class CameraService extends Service {

    private static final String TAG = "DoorSign";
    private static final String CHANNEL = "camera";
    private static final long RETRY_MS = 10_000;
    private static final int MAX_QUEUED = 600;             // about 2 seconds of packets at 1.5 Mbit/s: behind that, a viewer is dropped

    private static volatile String trouble;                // why it is not running, or null
    private static volatile String on;                     // the setup screen's line while it runs

    private final Handler main = new Handler(Looper.getMainLooper());
    private Engine engine;
    private CameraLogin running;

    /** Why the camera is not running (the card's words come from Panel.cameraCannot), or null while it runs or is starting. */
    static String trouble() {
        return trouble;
    }

    static void failedToStart(String why) {
        trouble = why;
    }

    /** The setup screen's line about the camera. */
    static String line(Context context) {
        Context app = context.getApplicationContext();
        String why = Panel.cameraCannot(Kiosk.isOwner(app), CameraLogin.load(new Prefs(app, "check_in")), trouble);
        if (why != null) return why;
        String now = on;
        return now != null ? now : "Camera starting";
    }

    @Override public IBinder onBind(Intent intent) {
        return null;
    }

    @Override public int onStartCommand(Intent intent, int flags, int startId) {
        foreground();
        CameraLogin login = CameraLogin.load(new Prefs(this, "check_in"));
        if (login == null || !Kiosk.isOwner(this)) {
            stopSelf();
            return START_NOT_STICKY;
        }
        if (!login.equals(running)) restart(login);          // a new password: the old stream's path and sessions go with the old engine
        return START_STICKY;
    }

    @Override public void onDestroy() {
        main.removeCallbacksAndMessages(null);
        if (engine != null) engine.stop();
        engine = null;
        running = null;
        on = null;
        super.onDestroy();
    }

    private void restart(CameraLogin login) {
        main.removeCallbacksAndMessages(null);
        if (engine != null) engine.stop();
        running = login;
        engine = new Engine(login);
        engine.start();
    }

    private void foreground() {
        NotificationManager notifications = getSystemService(NotificationManager.class);
        notifications.createNotificationChannel(new NotificationChannel(CHANNEL, "Door sign camera", NotificationManager.IMPORTANCE_MIN));
        Notification notification = new Notification.Builder(this, CHANNEL).setSmallIcon(R.drawable.ic_sign).setContentTitle("Door sign camera")
                .setContentText("UniFi Protect can record this panel's camera").setOngoing(true).build();
        if (Build.VERSION.SDK_INT >= 29) startForeground(1, notification, ServiceInfo.FOREGROUND_SERVICE_TYPE_CAMERA);
        else startForeground(1, notification);
    }

    /** One run of the camera and its three servers, with one login. Stopped as a whole, and replaced by a new one to try again. */
    private final class Engine implements CameraStream.Sink {
        private final CameraLogin login;
        private final String streamPath;
        private final StreamClients clients = new StreamClients(MAX_QUEUED);
        private final H264Rtp rtp = new H264Rtp(new SecureRandom().nextInt());
        private final CameraStream stream;
        private volatile boolean stopped;
        private volatile byte[] sps;
        private volatile byte[] pps;
        private volatile String sdp;
        private ServerSocket onvif;
        private ServerSocket rtsp;
        private MulticastSocket discovery;
        private WifiManager.MulticastLock multicastLock;
        private String host;
        private Onvif.Info info;
        private String urn;

        Engine(CameraLogin login) {
            this.login = login;
            this.streamPath = Onvif.streamPath(login.password);
            this.stream = new CameraStream(CameraService.this, this);
        }

        void start() {
            Identity identity = Identity.load(new Prefs(CameraService.this, "check_in"), new SecureRandom());
            Network network = getSystemService(ConnectivityManager.class).getActiveNetwork();
            LinkProperties link = network == null ? null : getSystemService(ConnectivityManager.class).getLinkProperties(network);
            host = ipv4(link);
            if (host == null) {
                failed("the panel has no network address");
                return;
            }
            info = new Onvif.Info(login, host, mac(link), Build.MODEL == null ? "" : Build.MODEL, versionName(), identity.id.substring(0, 12));
            urn = "urn:uuid:" + UUID.nameUUIDFromBytes(identity.id.getBytes(StandardCharsets.UTF_8));
            try {
                onvif = new ServerSocket(Onvif.PORT);
                rtsp = new ServerSocket(Onvif.RTSP_PORT);
                discovery = new MulticastSocket(Discovery.PORT);
                discovery.joinGroup(InetAddress.getByName(Discovery.GROUP));
            } catch (IOException e) {
                failed("a port is in use: " + e.getMessage());
                return;
            }
            WifiManager wifi = getSystemService(WifiManager.class);
            if (wifi != null) {
                multicastLock = wifi.createMulticastLock("door-sign-camera");
                multicastLock.acquire();
            }
            thread("door-sign-onvif", () -> accept(onvif, this::onvifConnection));
            thread("door-sign-rtsp", () -> accept(rtsp, this::rtspConnection));
            thread("door-sign-discovery", this::discover);
            stream.start();
            trouble = null;
            on = "Camera on: " + host + ", ONVIF port " + Onvif.PORT + ", user " + login.user;
            Log.i(TAG, on);
        }

        void stop() {
            if (stopped) return;
            stopped = true;
            stream.stop();
            clients.closeAll();
            close(onvif);
            close(rtsp);
            if (discovery != null) discovery.close();
            if (multicastLock != null && multicastLock.isHeld()) multicastLock.release();
        }

        // --- CameraStream.Sink, on the camera's thread

        @Override public void config(byte[] sps, byte[] pps) {
            this.sps = sps;
            this.pps = pps;
            this.sdp = Rtsp.sdp(host, sps, pps);
        }

        @Override public void frame(byte[] annexB, long presentationUs, boolean keyframe) {
            if (clients.size() == 0) return;
            List<byte[]> nals = H264Rtp.nals(annexB);
            if (keyframe && sps != null && nals.stream().noneMatch(n -> (n[0] & 0x1f) == 7)) {
                nals.add(0, pps);                // a viewer that starts here gets the parameter sets with the picture
                nals.add(0, sps);
            }
            boolean first = true;
            for (int i = 0; i < nals.size(); i++) {
                for (byte[] packet : rtp.packets(nals.get(i), presentationUs, i == nals.size() - 1)) {
                    clients.send(H264Rtp.interleaved(packet), keyframe && first);
                    first = false;
                }
            }
        }

        @Override public void failed(String why) {
            main.post(() -> {
                if (engine != this) return;
                Log.w(TAG, "Camera off: " + why);
                trouble = why;
                on = null;
                stop();
                main.postDelayed(() -> {
                    if (engine == this && running != null) restart(running);
                }, RETRY_MS);
            });
        }

        // --- the servers, each connection on a thread of its own

        private void accept(ServerSocket server, Connection handler) {
            while (!stopped) {
                try {
                    Socket socket = server.accept();
                    thread("door-sign-connection", () -> {
                        try {
                            handler.run(socket);
                        } catch (IOException | RuntimeException e) {
                            // the viewer left, or sent something that is not a request
                        } finally {
                            close(socket);
                        }
                    });
                } catch (IOException e) {
                    if (!stopped) failed("the camera's server stopped: " + e.getMessage());
                    return;
                }
            }
        }

        private void onvifConnection(Socket socket) throws IOException {
            socket.setSoTimeout(10_000);
            MiniHttp.Request request = MiniHttp.read(socket.getInputStream());
            if (!request.method.equals("POST") || !request.path.startsWith("/onvif/")) {
                MiniHttp.write(socket.getOutputStream(), 404, "text/plain", "Not found");
                return;
            }
            Onvif.Reply reply = Onvif.handle(request.body, info, Instant.now());
            if (reply.status == 400) Log.i(TAG, "ONVIF: Protect asked for " + reply.action + ", which the door sign does not answer");
            MiniHttp.write(socket.getOutputStream(), reply.status, "application/soap+xml; charset=utf-8", reply.xml);
        }

        private void rtspConnection(Socket socket) throws IOException {
            socket.setSoTimeout(30_000);
            InputStream in = socket.getInputStream();
            OutputStream out = new Atomic(socket.getOutputStream());
            String session = Long.toHexString(new SecureRandom().nextLong() & Long.MAX_VALUE).toUpperCase();
            while (!stopped) {
                Rtsp.Request request = Rtsp.read(in);
                String described = sdp;
                Rtsp.Reply reply = request.method.equals("DESCRIBE") && described == null
                        ? new Rtsp.Reply(503, request.headers.get("cseq"))         // the encoder has not started yet: Protect asks again
                        : Rtsp.answer(request, streamPath, described == null ? "" : described, session);
                out.write(reply.bytes());
                if (reply.close) return;
                if (reply.play) {
                    socket.setSoTimeout(0);      // from here a dead viewer shows as a full queue, not a quiet connection
                    clients.add(out);
                    stream.requestKeyframe();
                }
            }
        }

        private void discover() {
            byte[] buffer = new byte[16 * 1024];
            String xaddr = "http://" + host + ":" + Onvif.PORT + "/onvif/device_service";
            while (!stopped) {
                try {
                    DatagramPacket packet = new DatagramPacket(buffer, buffer.length);
                    discovery.receive(packet);
                    String probe = new String(packet.getData(), 0, packet.getLength(), StandardCharsets.UTF_8);
                    String match = Discovery.probeMatch(probe, urn, xaddr, login.name.isEmpty() ? "Door sign" : login.name, info.model);
                    if (match == null) continue;
                    byte[] answer = match.getBytes(StandardCharsets.UTF_8);
                    discovery.send(new DatagramPacket(answer, answer.length, packet.getSocketAddress()));
                } catch (IOException e) {
                    if (!stopped) failed("discovery stopped: " + e.getMessage());
                    return;
                }
            }
        }
    }

    private interface Connection {
        void run(Socket socket) throws IOException;
    }

    /** A connection's output written a whole buffer at a time: the stream's packets and the RTSP replies never interleave. */
    private static final class Atomic extends FilterOutputStream {
        Atomic(OutputStream out) {
            super(out);
        }

        @Override public synchronized void write(byte[] b, int off, int len) throws IOException {
            out.write(b, off, len);
            out.flush();
        }
    }

    private static void thread(String name, Runnable run) {
        Thread thread = new Thread(run, name);
        thread.setDaemon(true);
        thread.start();
    }

    private static void close(java.io.Closeable closeable) {
        try {
            if (closeable != null) closeable.close();
        } catch (IOException ignored) {
        }
    }

    private static String ipv4(LinkProperties link) {
        if (link == null) return null;
        for (LinkAddress address : link.getLinkAddresses()) {
            if (address.getAddress() instanceof Inet4Address) return address.getAddress().getHostAddress();
        }
        return null;
    }

    /** The network interface's MAC ("f8:7a:39:7c:f6:25"), or "" when Android won't say (it hides it from apps from Android 11). */
    private static String mac(LinkProperties link) {
        try {
            NetworkInterface nic = link == null || link.getInterfaceName() == null ? null : NetworkInterface.getByName(link.getInterfaceName());
            byte[] hw = nic == null ? null : nic.getHardwareAddress();
            if (hw != null && hw.length == 6) return String.format("%02x:%02x:%02x:%02x:%02x:%02x", hw[0], hw[1], hw[2], hw[3], hw[4], hw[5]);
            java.io.File file = new java.io.File("/sys/class/net/" + link.getInterfaceName() + "/address");
            try (java.io.BufferedReader r = new java.io.BufferedReader(new java.io.FileReader(file))) {
                String line = r.readLine();
                if (line != null && line.trim().matches("([0-9a-f]{2}:){5}[0-9a-f]{2}")) return line.trim();
            }
        } catch (Exception e) {
            // hidden from this app
        }
        return "";
    }

    private String versionName() {
        try {
            String name = getPackageManager().getPackageInfo(getPackageName(), 0).versionName;
            return name == null ? "" : name;
        } catch (Exception e) {
            return "";
        }
    }
}
