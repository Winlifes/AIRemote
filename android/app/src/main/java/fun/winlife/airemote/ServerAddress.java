package fun.winlife.airemote;

import java.net.URI;
import java.util.Locale;

final class ServerAddress {
    private static int port(URI uri) { return uri.getPort() == -1 ? 443 : uri.getPort(); }
    static String normalize(String value) {
        URI uri = URI.create(value.trim());
        if (!"https".equalsIgnoreCase(uri.getScheme()) || uri.getHost() == null || uri.getUserInfo() != null ||
            uri.getQuery() != null || uri.getFragment() != null || port(uri) < 1 || port(uri) > 65535 ||
            !(uri.getPath() == null || uri.getPath().isEmpty() || uri.getPath().equals("/")))
            throw new IllegalArgumentException("请输入 HTTPS 服务器根地址");
        return "https://" + uri.getHost().toLowerCase(Locale.ROOT) + (port(uri) == 443 ? "" : ":" + port(uri));
    }
    static boolean sameOrigin(String address, String server) {
        URI a = URI.create(address), b = URI.create(server);
        return "https".equalsIgnoreCase(a.getScheme()) && a.getUserInfo() == null &&
            b.getHost().equalsIgnoreCase(a.getHost()) && port(a) == port(b);
    }
}
