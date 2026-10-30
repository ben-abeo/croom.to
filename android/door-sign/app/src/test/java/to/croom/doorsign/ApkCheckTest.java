package to.croom.doorsign;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertNull;

import java.util.Collections;
import java.util.HashSet;
import java.util.Set;
import org.junit.Test;

public class ApkCheckTest {

    static final String PKG = "to.croom.doorsign";
    static final Set<String> KEY = Collections.singleton("a".repeat(64));
    static final Set<String> OTHER = Collections.singleton("b".repeat(64));

    @Test public void theDownloadIsTheOneTheTvPiNamed() {
        assertNull(ApkCheck.download(10, "x", 10, "x"));
        assertEquals("The download is 9 bytes, not the 10 the TV Pi said", ApkCheck.download(9, "x", 10, "x"));
        assertEquals("The download is not the APK the TV Pi said (its SHA-256 differs)", ApkCheck.download(10, "y", 10, "x"));
        assertEquals("e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855", ApkCheck.sha256(new byte[0]));
    }

    @Test public void onlyTheSameAppNewerAndSignedWithTheSameKey() {
        assertNull(ApkCheck.app(PKG, 2, KEY, 3, PKG, 3, new HashSet<>(KEY)));
        assertEquals("The download is not an app Android can read", ApkCheck.app(PKG, 2, KEY, 3, null, -1, null));
        assertEquals("The download is not the door sign app (it is com.example.evil)", ApkCheck.app(PKG, 2, KEY, 3, "com.example.evil", 3, KEY));
        assertEquals("The download is version 4, not version 3", ApkCheck.app(PKG, 2, KEY, 3, PKG, 4, KEY));
        assertEquals("The door sign has version 3 already: Android installs only a newer one", ApkCheck.app(PKG, 3, KEY, 3, PKG, 3, KEY));
        assertEquals("The door sign has version 5 already: Android installs only a newer one", ApkCheck.app(PKG, 5, KEY, 3, PKG, 3, KEY));
        assertEquals("The download is not signed", ApkCheck.app(PKG, 2, KEY, 3, PKG, 3, Collections.emptySet()));
        assertEquals("The download is signed with another key than the door sign app on this panel", ApkCheck.app(PKG, 2, KEY, 3, PKG, 3, OTHER));
        Set<String> both = new HashSet<>(KEY);
        both.addAll(OTHER);
        assertEquals("The download is signed with another key than the door sign app on this panel", ApkCheck.app(PKG, 2, KEY, 3, PKG, 3, both));
    }
}
