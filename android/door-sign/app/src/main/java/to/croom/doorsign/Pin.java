package to.croom.doorsign;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.security.SecureRandom;

/** The PIN that opens the setup screen, kept as a salted hash, never as itself. */
final class Pin {

    private Pin() {}

    static final class Stored {
        final String salt;
        final String hash;

        Stored(String salt, String hash) {
            this.salt = salt;
            this.hash = hash;
        }
    }

    static boolean valid(String pin) {
        return pin != null && pin.matches("[0-9]{4,8}");
    }

    static Stored store(String pin) {
        byte[] bytes = new byte[16];
        new SecureRandom().nextBytes(bytes);
        String salt = hex(bytes);
        return new Stored(salt, hash(salt, pin));
    }

    static boolean matches(String pin, Stored stored) {
        if (pin == null || stored == null) return false;
        return MessageDigest.isEqual(hash(stored.salt, pin).getBytes(StandardCharsets.US_ASCII),
                stored.hash.getBytes(StandardCharsets.US_ASCII));
    }

    private static String hash(String salt, String pin) {
        try {
            MessageDigest digest = MessageDigest.getInstance("SHA-256");
            return hex(digest.digest((salt + ":" + pin).getBytes(StandardCharsets.UTF_8)));
        } catch (NoSuchAlgorithmException e) {
            throw new IllegalStateException(e);
        }
    }

    private static String hex(byte[] bytes) {
        StringBuilder text = new StringBuilder();
        for (byte b : bytes) text.append(String.format("%02x", b));
        return text.toString();
    }
}
