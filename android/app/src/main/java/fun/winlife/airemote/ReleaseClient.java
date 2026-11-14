package fun.winlife.airemote;

import org.json.JSONArray;
import org.json.JSONObject;
import java.io.*;
import java.net.*;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.Locale;

final class ReleaseClient {
    static final long MAX_APK = 80L * 1024 * 1024;
    record Release(String version, String url, String digest) {}

    static HttpURLConnection github(String address) throws IOException {
        for (int redirects = 0; redirects < 6; redirects++) {
            URL url = new URL(address);
            String host = url.getHost().toLowerCase(Locale.ROOT);
            if (!url.getProtocol().equals("https") || url.getUserInfo() != null ||
                !(host.equals("github.com") || host.equals("api.github.com") || host.endsWith(".githubusercontent.com")))
                throw new IOException("更新地址不是受信任的 GitHub HTTPS 地址");
            HttpURLConnection connection = (HttpURLConnection) url.openConnection();
            connection.setInstanceFollowRedirects(false);
            connection.setConnectTimeout(15000); connection.setReadTimeout(30000);
            connection.setRequestProperty("User-Agent", "AIRemote-Android/" + BuildConfig.VERSION_NAME);
            int status = connection.getResponseCode();
            if (status >= 300 && status < 400) {
                String next = connection.getHeaderField("Location"); connection.disconnect();
                if (next == null) throw new IOException("GitHub 返回了无效跳转");
                address = new URL(url, next).toString(); continue;
            }
            if (status != 200) { connection.disconnect(); throw new IOException("GitHub 暂不可用 (" + status + ")"); }
            return connection;
        }
        throw new IOException("更新跳转过多");
    }
    static byte[] read(String url, int maximum) throws IOException {
        HttpURLConnection connection = github(url);
        try (InputStream input = connection.getInputStream(); ByteArrayOutputStream output = new ByteArrayOutputStream()) {
            byte[] buffer = new byte[16384]; int n;
            while ((n = input.read(buffer)) != -1) {
                if (output.size() + n > maximum) throw new IOException("更新元数据过大");
                output.write(buffer, 0, n);
            }
            return output.toByteArray();
        } finally { connection.disconnect(); }
    }
    static Release latest() throws Exception {
        JSONObject json = new JSONObject(new String(read("https://api.github.com/repos/" + BuildConfig.RELEASE_REPO + "/releases/latest", 1024 * 1024), StandardCharsets.UTF_8));
        String version = json.getString("tag_name");
        if (json.optBoolean("draft") || json.optBoolean("prerelease") || ReleaseVersion.code(version) < 0)
            throw new IOException("未找到稳定版本");
        String apk = null, checksums = null, digest = null;
        JSONArray assets = json.getJSONArray("assets");
        for (int i = 0; i < assets.length(); i++) {
            JSONObject asset = assets.getJSONObject(i);
            if (asset.getString("name").equals("AIRemote-android.apk")) {
                apk = asset.getString("browser_download_url"); digest = asset.optString("digest", "");
            }
            if (asset.getString("name").equals("SHA256SUMS")) checksums = asset.getString("browser_download_url");
        }
        if (apk == null) throw new IOException("此版本缺少 Android 安装包");
        String base = "https://github.com/" + BuildConfig.RELEASE_REPO + "/releases/download/" + version + "/";
        if (!apk.equals(base + "AIRemote-android.apk") || (checksums != null && !checksums.equals(base + "SHA256SUMS")))
            throw new IOException("更新文件不属于当前仓库版本");
        if (digest != null && digest.matches("sha256:[a-fA-F0-9]{64}")) digest = digest.substring(7);
        else {
            digest = null;
            if (checksums != null) for (String line : new String(read(checksums, 65536), StandardCharsets.UTF_8).split("\\R")) {
                String[] pieces = line.trim().split("\\s+", 2);
                if (pieces.length == 2 && pieces[1].replaceFirst("^\\*", "").equals("AIRemote-android.apk")) digest = pieces[0];
            }
        }
        if (digest == null || !digest.matches("[a-fA-F0-9]{64}")) throw new IOException("安装包缺少 SHA-256 校验值");
        return new Release(version, apk, digest.toLowerCase(Locale.ROOT));
    }
    static File download(Release release, File directory) throws Exception {
        if (!directory.exists() && !directory.mkdirs()) throw new IOException("无法创建更新目录");
        File file = new File(directory, "AIRemote-update.apk"), partial = new File(directory, "AIRemote-update.partial");
        MessageDigest digest = MessageDigest.getInstance("SHA-256");
        HttpURLConnection connection = github(release.url());
        try (InputStream input = connection.getInputStream(); OutputStream output = new FileOutputStream(partial)) {
            byte[] buffer = new byte[32768]; int n; long total = 0;
            while ((n = input.read(buffer)) != -1) {
                total += n; if (total > MAX_APK) throw new IOException("安装包过大");
                output.write(buffer, 0, n); digest.update(buffer, 0, n);
            }
        } finally { connection.disconnect(); }
        StringBuilder actual = new StringBuilder(); for (byte b : digest.digest()) actual.append(String.format(Locale.ROOT, "%02x", b & 255));
        if (!actual.toString().equals(release.digest())) { partial.delete(); throw new IOException("安装包校验失败，已停止安装"); }
        if (file.exists() && !file.delete()) throw new IOException("无法替换旧的更新缓存");
        if (!partial.renameTo(file)) throw new IOException("无法保存更新文件");
        return file;
    }
}
