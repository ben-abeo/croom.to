package to.croom.doorsign;

import java.net.URI;
import java.net.URISyntaxException;
import java.util.Objects;

/** The address of the door sign page, as typed on the setup screen. */
final class SignAddress {

    private SignAddress() {}

    /**
     * The sign's full address for what was typed: "10.0.0.5:8080" is http://10.0.0.5:8080/sign, a bare server is its /sign, and
     * a full address is kept. Null for anything that is not an http or https address with a host.
     */
    static String normalize(String typed) {
        if (typed == null) return null;
        String text = typed.trim();
        if (text.isEmpty()) return null;
        if (!text.contains("://")) text = "http://" + text;
        URI uri;
        try {
            uri = new URI(text);
        } catch (URISyntaxException e) {
            return null;
        }
        String scheme = uri.getScheme();
        if (!"http".equals(scheme) && !"https".equals(scheme)) return null;
        if (uri.getHost() == null || uri.getHost().isEmpty()) return null;
        String path = uri.getRawPath();
        if (path == null || path.isEmpty() || path.equals("/")) path = "/sign";
        String query = uri.getRawQuery() == null ? "" : "?" + uri.getRawQuery();
        String port = uri.getPort() == -1 ? "" : ":" + uri.getPort();
        return scheme + "://" + uri.getHost() + port + path + query;
    }

    /** The room's address for the sign's: its scheme, host and port ("http://10.0.0.5:8080"), where the check-in goes; null for none. */
    static String server(String sign) {
        if (sign == null) return null;
        try {
            URI uri = new URI(sign);
            if (uri.getScheme() == null || uri.getHost() == null || uri.getHost().isEmpty()) return null;
            return uri.getScheme() + "://" + uri.getHost() + (uri.getPort() == -1 ? "" : ":" + uri.getPort());
        } catch (URISyntaxException e) {
            return null;
        }
    }

    /** Whether `url` is on the sign's own server (scheme, host and port): the only place the page, and the light bar's hook, may go. */
    static boolean sameServer(String sign, String url) {
        try {
            URI a = new URI(sign);
            URI b = new URI(url);
            return Objects.equals(a.getScheme(), b.getScheme())
                    && a.getHost() != null
                    && a.getHost().equalsIgnoreCase(b.getHost())
                    && a.getPort() == b.getPort();
        } catch (URISyntaxException e) {
            return false;
        }
    }
}
