package fun.winlife.airemote;
import org.junit.Test;
import static org.junit.Assert.*;
public class ReleaseVersionTest {
    @Test public void stableReleasesCompareNumerically() {
        assertTrue(ReleaseVersion.code("v0.10.0") > ReleaseVersion.code("0.9.9"));
        assertEquals(6000, ReleaseVersion.code("v0.6.0"));
    }
    @Test public void prereleasesAndMalformedTagsCannotUpdateStable() {
        for (String value : new String[]{"v0.6.1-beta", "v1.2", "../../v1.0.0", "v1.1000.0", "latest"})
            assertEquals(-1, ReleaseVersion.code(value));
    }
}
