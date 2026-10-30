package to.croom.doorsign;

import java.security.SecureRandom;
import java.util.Base64;

/**
 * The door sign's id and token for checking in with the room's TV Pi (room settings spec 2026-10-29, section 7). Both are random, made
 * at the first start and kept in the app's private storage; the token goes with every check-in from the first, the TV Pi keeps only
 * its hash, and Allow on Settings › Software lets this id with this token in. A new id (clearing the app's data) asks to join again.
 */
final class Identity {

    static final String ID = "checkin_id";
    static final String TOKEN = "checkin_token";

    final String id;
    final String token;

    private Identity(String id, String token) {
        this.id = id;
        this.token = token;
    }

    /** The identity kept in `store`, made and kept there the first time. */
    static synchronized Identity load(Store store, SecureRandom random) {
        String id = store.get(ID);
        String token = store.get(TOKEN);
        if (id == null || !id.matches("[0-9a-f]{32}") || token == null || !token.matches("[A-Za-z0-9_-]{43}")) {
            id = newId(random);
            token = newToken(random);
            store.put(TOKEN, token);
            store.put(ID, id);
        }
        return new Identity(id, token);
    }

    /** 32 hex characters (128 random bits). */
    static String newId(SecureRandom random) {
        byte[] bytes = new byte[16];
        random.nextBytes(bytes);
        StringBuilder text = new StringBuilder();
        for (byte b : bytes) text.append(String.format("%02x", b));
        return text.toString();
    }

    /** 43 URL-safe characters (256 random bits). */
    static String newToken(SecureRandom random) {
        byte[] bytes = new byte[32];
        random.nextBytes(bytes);
        return Base64.getUrlEncoder().withoutPadding().encodeToString(bytes);
    }
}
