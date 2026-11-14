package fun.winlife.airemote;

/** Stable releases only. Pre-releases never replace the stable channel. */
public final class ReleaseVersion {
    private ReleaseVersion() {}
    public static long code(String value) {
        if (value == null || !value.matches("v?\\d{1,3}\\.\\d{1,3}\\.\\d{1,3}")) return -1;
        String[] parts = value.replaceFirst("^v", "").split("\\.");
        return Long.parseLong(parts[0]) * 1000000L + Long.parseLong(parts[1]) * 1000L + Long.parseLong(parts[2]);
    }
}
