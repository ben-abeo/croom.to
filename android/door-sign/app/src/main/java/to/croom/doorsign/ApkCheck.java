package to.croom.doorsign;

import java.io.File;
import java.io.FileInputStream;
import java.io.IOException;
import java.io.InputStream;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.util.Set;

/**
 * What the door sign checks before it installs an update (room settings spec 2026-10-29, section 6): the download is the size and the
 * SHA-256 the TV Pi said; it is this app (the same package), the version the job named and newer than the one installed (Android
 * installs no older one over a newer one); and it is signed with the same key as the app installed (each signer's SHA-256). Nothing is
 * installed unless every check passes.
 */
final class ApkCheck {

    private ApkCheck() {}

    /** Why the downloaded file is not the one the TV Pi named, or null. */
    static String download(long size, String sha256, long wantedSize, String wantedSha256) {
        if (size != wantedSize) return "The download is " + size + " bytes, not the " + wantedSize + " the TV Pi said";
        if (!sha256.equals(wantedSha256)) return "The download is not the APK the TV Pi said (its SHA-256 differs)";
        return null;
    }

    /** Why the APK must not be installed over the app that runs, or null. */
    static String app(String ownPackage, long installedCode, Set<String> installedSigners, int wantedCode, String apkPackage,
                      long apkCode, Set<String> apkSigners) {
        if (apkPackage == null) return "The download is not an app Android can read";
        if (!ownPackage.equals(apkPackage)) return "The download is not the door sign app (it is " + apkPackage + ")";
        if (apkCode != wantedCode) return "The download is version " + apkCode + ", not version " + wantedCode;
        if (apkCode <= installedCode) {
            return "The door sign has version " + installedCode + " already: Android installs only a newer one";
        }
        if (apkSigners == null || apkSigners.isEmpty()) return "The download is not signed";
        if (installedSigners == null || installedSigners.isEmpty() || !installedSigners.equals(apkSigners)) {
            return "The download is signed with another key than the door sign app on this panel";
        }
        return null;
    }

    static String sha256(byte[] bytes) {
        try {
            return hex(MessageDigest.getInstance("SHA-256").digest(bytes));
        } catch (NoSuchAlgorithmException e) {
            throw new IllegalStateException(e);
        }
    }

    static String sha256(File file) throws IOException {
        try (InputStream in = new FileInputStream(file)) {
            MessageDigest digest = MessageDigest.getInstance("SHA-256");
            byte[] buffer = new byte[64 * 1024];
            for (int n; (n = in.read(buffer)) > 0; ) digest.update(buffer, 0, n);
            return hex(digest.digest());
        } catch (NoSuchAlgorithmException e) {
            throw new IllegalStateException(e);
        }
    }

    static String hex(byte[] bytes) {
        StringBuilder text = new StringBuilder();
        for (byte b : bytes) text.append(String.format("%02x", b));
        return text.toString();
    }
}
