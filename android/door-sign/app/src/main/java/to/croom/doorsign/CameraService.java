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
import android.Manifest;
import android.content.pm.PackageManager;
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
 * While the room's Door sign camera sound is on, the microphone (SoundStream) goes with the picture (spec 2026-11-10): a microphone
 * that fails turns only the sound off, and says so; the picture carries on.
 */
public class CameraService extends Service {

    private static final String TAG = "DoorSign";
    private static final String CHANNEL = "camera";
    private static final long RETRY_MS = 10_000;
    private static final int MAX_QUEUED = 600;             // about 2 seconds of packets at 1.5 Mbit/s: behind that, a viewer is dropped
    private static final int MAX_CONNECTIONS = 8;          // per server: more at once are turned away, so the LAN can't fill the process
    private static final long STALL_MS = 15_000;           // no picture from the camera for this long: start it again

    private static volatile String trouble;                // why it is not running, or null
    private static volatile String on;                     // the setup screen's line while it runs
    private static volatile String soundTrouble;           // why the room's sound is not being sent, or null

    private final Handler main = new Handler(Looper.getMainLooper());
    private Engine engine;
    private CameraLogin running;
    private boolean runningSound;

    /** Why the camera is not running (the card's words come from Panel.cameraCannot), or null while it runs or is starting. */
    static String trouble() {
        return trouble;
    }

    /** Why the room's sound is not being sent (the card's words come from Panel.soundCannot), or null. */
    static String soundTrouble() {
        return soundTrouble;
    }

    static void failedToStart(String why) {
        trouble = why;
    }

    /** The setup screen's line about the camera, and its sound. */
    static String line(Context context) {
        Context app = context.getApplicationContext();
        CameraLogin login = CameraLogin.load(new Prefs(app, "check_in"));
        String why = Panel.cameraCannot(Kiosk.isOwner(app), login, trouble);
        if (why != null) return why;
        String now = on;
        if (now == null) return "Camera starting";
        String sound = Panel.soundCannot(login, soundTrouble);
        return sound == null ? now : now + ". " + sound;
    }

    @Override public IBinder onBind(Intent intent) {
        return null;
    }

    @Override public int onStartCommand(Intent intent, int flags, int startId) {
        CameraLogin login = CameraLogin.load(new Prefs(this, "check_in"));
        boolean granted = checkSelfPermission(Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED;
        String soundRefused = login == null || !login.sound ? null : granted ? null : "Android has not granted the app the microphone";
        try {
            soundRefused = foreground(soundRefused == null && login != null && login.sound, soundRefused);
        } catch (RuntimeException e) {          // nothing in the camera may end the sign's process
            failedToStart("Android would not run it: " + e.getMessage());
            stopSelf();
            return START_NOT_STICKY;
        }
        if (login == null || !Kiosk.isOwner(this)) {
            stopSelf();
            return START_NOT_STICKY;
        }
        boolean sound = login.sound && soundRefused == null;
        if (!login.sound) soundTrouble = null;                   // the room records no sound: nothing to say
        else if (soundRefused != null) soundTrouble = soundRefused;   // else the running engine's word stands (a microphone that failed)
        if (!login.equals(running) || sound != runningSound) {   // a new password: the old stream's path and sessions go with the old engine
            try {
                restart(login, sound);
            } catch (RuntimeException e) {
                failedToStart("the camera did not start: " + e.getMessage());
                if (engine != null) engine.stop();
                engine = null;
                running = null;                 // the next ensure (the sign coming to the front, a check-in) tries again
            }
        }
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

    private void restart(CameraLogin login, boolean sound) {
        main.removeCallbacksAndMessages(null);
        if (engine != null) engine.stop();
        running = login;
        runningSound = sound;
        engine = new Engine(login, sound);
        engine.start();
    }

    /**
     * A foreground service of the camera type, and of the microphone type too when it is to send sound (from Android 11 Android
     * silences a microphone used outside one). Returns why the sound can't go (`refused`, or Android refusing the microphone type).
     */
    private String foreground(boolean sound, String refused) {
        NotificationManager notifications = getSystemService(NotificationManager.class);
        notifications.createNotificationChannel(new NotificationChannel(CHANNEL, "Door sign camera", NotificationManager.IMPORTANCE_MIN));
        Notification notification = new Notification.Builder(this, CHANNEL).setSmallIcon(R.drawable.ic_sign).setContentTitle("Door sign camera")
                .setContentText(sound ? "UniFi Protect can record this panel's camera and microphone" : "UniFi Protect can record this panel's camera")
                .setOngoing(true).build();
        if (Build.VERSION.SDK_INT < 30) {                    // the camera and microphone types are from Android 11
            startForeground(1, notification);
            return refused;
        }
        if (sound) {
            try {
                startForeground(1, notification, ServiceInfo.FOREGROUND_SERVICE_TYPE_CAMERA | ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE);
                return null;
            } catch (RuntimeException e) {                   // the picture without the sound, rather than nothing
                refused = "Android would not let the app use the microphone: " + e.getMessage();
            }
        }
        startForeground(1, notification, ServiceInfo.FOREGROUND_SERVICE_TYPE_CAMERA);
        return refused;
    }

    /** One run of the camera and its three servers, with one login. Stopped as a whole, and replaced by a new one to try again. */
    private final class Engine implements CameraStream.Sink, SoundStream.Sink {
        private final CameraLogin login;
        private final String streamPath;
        private final StreamClients clients = new StreamClients(MAX_QUEUED);
        private final H264Rtp rtp = new H264Rtp(new SecureRandom().nextInt());
        private final CameraStream stream;
        private final SoundStream sound;        // null: picture only
        private final AacRtp aac = new AacRtp(new SecureRandom().nextInt());
        private volatile boolean stopped;
        private volatile boolean soundGone;     // the microphone failed: picture only from here
        private volatile byte[] sps;
        private volatile byte[] pps;
        private volatile byte[] aacConfig;
        private ServerSocket onvif;
        private ServerSocket rtsp;
        private MulticastSocket discovery;
        private WifiManager.MulticastLock multicastLock;
        private String host;                    // at start, for the setup screen's line; each connection uses the address it came in on
        private String mac;
        private String serial;
        private String urn;
        private volatile long lastFrame;
        private volatile long lastSound;

        Engine(CameraLogin login, boolean sound) {
            this.login = login;
            this.streamPath = Onvif.streamPath(login.password);
            this.stream = new CameraStream(CameraService.this, this);
            this.sound = sound ? new SoundStream(this) : null;
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
            mac = mac(link);
            serial = identity.id.substring(0, 12);
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
            lastFrame = System.nanoTime();
            lastSound = lastFrame;
            stream.start();
            if (sound != null) {
                sound.start();
                soundTrouble = null;
            }
            main.postDelayed(this::watch, STALL_MS);
            trouble = null;
            on = "Camera on: " + host + ", ONVIF port " + Onvif.PORT + ", user " + login.user + (sound != null ? ", with sound" : "");
            Log.i(TAG, on);
        }

        void stop() {
            if (stopped) return;
            stopped = true;
            stream.stop();
            if (sound != null) sound.stop();
            clients.closeAll();
            close(onvif);
            close(rtsp);
            if (discovery != null) discovery.close();
            if (multicastLock != null && multicastLock.isHeld()) multicastLock.release();
        }

        /** A camera that stops sending pictures without saying so leaves Protect recording nothing: start it again, and say why. */
        private void watch() {
            if (engine != this || stopped) return;
            if (System.nanoTime() - lastFrame > STALL_MS * 1_000_000) {
                failed("no picture from the camera for " + STALL_MS / 1000 + " seconds");
                return;
            }
            // A microphone that opened but gives nothing would hold every DESCRIBE (and so the picture) waiting for its config
            if (sound != null && !soundGone && System.nanoTime() - lastSound > STALL_MS * 1_000_000) {
                soundFailed("no sound from it for " + STALL_MS / 1000 + " seconds");
            }
            main.postDelayed(this::watch, STALL_MS);
        }

        private Onvif.Info info(String at) {
            CameraLogin sent = sound != null && !soundGone ? login : new CameraLogin(login.user, login.password, login.name, false);
            return new Onvif.Info(sent, at, mac, Build.MODEL == null ? "" : Build.MODEL, versionName(), serial);
        }

        // --- CameraStream.Sink, on the camera's thread

        @Override public void config(byte[] sps, byte[] pps) {
            this.pps = pps;
            this.sps = sps;
        }

        @Override public void frame(byte[] annexB, long presentationUs, boolean keyframe) {
            lastFrame = System.nanoTime();
            if (clients.size() == 0) return;
            List<byte[]> nals = H264Rtp.nals(annexB);
            if (keyframe && sps != null && nals.stream().noneMatch(n -> (n[0] & 0x1f) == 7)) {
                nals.add(0, pps);                // a viewer that starts here gets the parameter sets with the picture
                nals.add(0, sps);
            }
            boolean first = true;
            for (int i = 0; i < nals.size(); i++) {
                for (byte[] packet : rtp.packets(nals.get(i), presentationUs, i == nals.size() - 1)) {
                    clients.send(H264Rtp.interleaved(packet, 0), keyframe && first);
                    first = false;
                }
            }
        }

        // --- SoundStream.Sink, on the sound's thread

        @Override public void soundConfig(byte[] audioSpecificConfig) {
            lastSound = System.nanoTime();
            aacConfig = audioSpecificConfig;
        }

        // ponytail: no RTCP sender reports, so a viewer lines the sound up with the picture by when each began (both start at its first
        // keyframe, within a frame of each other); send RTCP SRs from one clock if Protect ever plays them out of step
        @Override public void sound(byte[] frame, long presentationUs) {
            lastSound = System.nanoTime();
            if (clients.size() == 0) return;
            clients.sendSound(H264Rtp.interleaved(aac.packet(frame, presentationUs), 2));
        }

        @Override public void soundFailed(String why) {
            main.post(() -> {
                if (engine != this || stopped) return;
                Log.w(TAG, "Sound off: " + why);
                soundGone = true;                  // new viewers are described the picture only; it is tried again when the camera next starts
                sound.stop();
                soundTrouble = "the microphone did not start: " + why;
                on = "Camera on: " + host + ", ONVIF port " + Onvif.PORT + ", user " + login.user;
            });
        }

        @Override public void failed(String why) {
            main.post(() -> {
                if (engine != this) return;
                Log.w(TAG, "Camera off: " + why);
                trouble = why;
                on = null;
                stop();
                main.postDelayed(() -> {
                    if (engine == this && running != null) restart(running, runningSound);
                }, RETRY_MS);
            });
        }

        // --- the servers, each connection on a thread of its own

        private void accept(ServerSocket server, Acceptor.Handler handler) {
            try {
                Acceptor.serve(server, MAX_CONNECTIONS, "door-sign-connection", handler);
            } catch (IOException e) {
                if (!stopped) failed("the camera's server stopped: " + e.getMessage());
            }
        }

        private void onvifConnection(Socket socket) throws IOException {
            socket.setSoTimeout(10_000);
            MiniHttp.Request request = MiniHttp.read(socket.getInputStream());
            if (!request.method.equals("POST") || !request.path.startsWith("/onvif/")) {
                MiniHttp.write(socket.getOutputStream(), 404, "text/plain", "Not found");
                return;
            }
            Onvif.Reply reply = Onvif.handle(request.body, info(socket.getLocalAddress().getHostAddress()), Instant.now());
            if (reply.status == 400) Log.i(TAG, "ONVIF: Protect asked for " + reply.action + ", which the door sign does not answer");
            MiniHttp.write(socket.getOutputStream(), reply.status, "application/soap+xml; charset=utf-8", reply.xml);
        }

        private void rtspConnection(Socket socket) throws IOException {
            socket.setSoTimeout(30_000);
            InputStream in = socket.getInputStream();
            OutputStream out = new Atomic(socket.getOutputStream());
            String session = Long.toHexString(new SecureRandom().nextLong() & Long.MAX_VALUE).toUpperCase();
            boolean withSound = false;
            while (!stopped) {
                Rtsp.Request request = Rtsp.read(in);
                byte[] s = sps;
                byte[] p = pps;
                byte[] a = sound == null || soundGone ? null : aacConfig;
                boolean soundPending = sound != null && !soundGone && a == null;     // its encoder has not started yet
                String described = s == null || p == null || soundPending ? null : Rtsp.sdp(socket.getLocalAddress().getHostAddress(), s, p, a);
                Rtsp.Reply reply = request.method.equals("DESCRIBE") && described == null
                        ? new Rtsp.Reply(503, request.headers.get("cseq"))         // the encoder has not started yet: Protect asks again
                        : Rtsp.answer(request, streamPath, described == null ? "" : described, session);
                out.write(reply.bytes());
                if (reply.close) return;
                if (reply.sound && reply.status == 200) withSound = true;
                if (reply.play) {
                    socket.setSoTimeout(0);      // from here a dead viewer shows as a full queue, not a quiet connection
                    clients.add(out, withSound);
                    stream.requestKeyframe();
                }
            }
        }

        private void discover() {
            byte[] buffer = new byte[16 * 1024];
            while (!stopped) {
                try {
                    DatagramPacket packet = new DatagramPacket(buffer, buffer.length);
                    discovery.receive(packet);
                    String probe = new String(packet.getData(), 0, packet.getLength(), StandardCharsets.UTF_8);
                    String now = currentHost();            // the address the panel has now, not the one it started with
                    if (now == null) continue;
                    String xaddr = "http://" + now + ":" + Onvif.PORT + "/onvif/device_service";
                    String match = Discovery.probeMatch(probe, urn, xaddr, login.name.isEmpty() ? "Door sign" : login.name,
                            Build.MODEL == null ? "" : Build.MODEL);
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

    private String currentHost() {
        ConnectivityManager connectivity = getSystemService(ConnectivityManager.class);
        Network network = connectivity.getActiveNetwork();
        return network == null ? null : ipv4(connectivity.getLinkProperties(network));
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
