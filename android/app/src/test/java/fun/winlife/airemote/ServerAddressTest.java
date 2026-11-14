package fun.winlife.airemote;
import org.junit.Test;
import static org.junit.Assert.*;

public class ServerAddressTest {
    @Test public void normalizesDefaultPortWithoutBreakingDownloads() {
        assertEquals("https://remote.example.com", ServerAddress.normalize(" HTTPS://Remote.Example.COM:443/ "));
        assertTrue(ServerAddress.sameOrigin("https://remote.example.com/api/requests/test/result", "https://remote.example.com:443"));
        assertFalse(ServerAddress.sameOrigin("https://remote.example.com:8443/file", "https://remote.example.com"));
        assertFalse(ServerAddress.sameOrigin("https://remote.example.com.evil.org/file", "https://remote.example.com"));
    }
    @Test public void rejectsInsecureOriginsCredentialsAndNonRootUrls() {
        for (String value : new String[]{"http://remote.example.com", "https://user:password@remote.example.com", "https://remote.example.com/app", "https://remote.example.com?key=value", "https://remote.example.com:99999"}) {
            try { ServerAddress.normalize(value); fail(value); } catch (IllegalArgumentException expected) {}
        }
    }
}
