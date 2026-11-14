package fun.winlife.airemote;

import android.app.DownloadManager;
import android.content.Intent;
import android.content.pm.PackageInfo;
import android.content.pm.PackageManager;
import android.content.pm.Signature;
import android.graphics.Color;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Environment;
import android.provider.Settings;
import android.webkit.CookieManager;
import androidx.annotation.NonNull;
import androidx.core.content.FileProvider;
import io.flutter.embedding.android.FlutterActivity;
import io.flutter.embedding.engine.FlutterEngine;
import io.flutter.plugin.common.MethodCall;
import io.flutter.plugin.common.MethodChannel;
import java.io.File;
import java.net.URI;
import java.security.MessageDigest;
import java.util.HashMap;
import java.util.Map;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

/** Flutter renders the entire interface. This bridge only performs OS actions. */
public final class MainActivity extends FlutterActivity {
    private final ExecutorService background = Executors.newSingleThreadExecutor();
    @Override protected void onCreate(Bundle state) {
        super.onCreate(state);
        getWindow().setStatusBarColor(Color.TRANSPARENT);
        getWindow().setNavigationBarColor(Color.TRANSPARENT);
        if (Build.VERSION.SDK_INT >= 29) {
            getWindow().setStatusBarContrastEnforced(false);
            getWindow().setNavigationBarContrastEnforced(false);
        }
    }
    @Override public void configureFlutterEngine(@NonNull FlutterEngine engine) {
        super.configureFlutterEngine(engine);
        new MethodChannel(engine.getDartExecutor().getBinaryMessenger(), "fun.winlife.airemote/native")
            .setMethodCallHandler(this::handle);
    }
    private void handle(MethodCall call, MethodChannel.Result result) {
        try {
            switch (call.method) {
                case "legacySession": {
                    // One-time migration; no WebView is displayed or created.
                    String server = getSharedPreferences("airemote", MODE_PRIVATE).getString("server", "");
                    Map<String, Object> session = new HashMap<>();
                    if (!server.isEmpty()) {
                        server = ServerAddress.normalize(server);
                        session.put("server", server);
                        String cookies = CookieManager.getInstance().getCookie(server);
                        if (cookies != null) for (String cookie : cookies.split(";")) {
                            String name = cookie.trim().split("=", 2)[0];
                            if (name.equals("__Host-airemote_session") || name.equals("airemote_session")) session.put("cookie", cookie.trim());
                        }
                    }
                    result.success(session); break;
                }
                case "downloadFile": enqueueDownload(call); result.success(null); break;
                case "verifyUpdate": verifyAsync(call.argument("path"), false, result); break;
                case "installUpdate": verifyAsync(call.argument("path"), true, result); break;
                case "canInstallUpdates": result.success(canInstall()); break;
                case "openInstallPermission":
                    startActivity(new Intent(Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES, Uri.parse("package:" + getPackageName())));
                    result.success(null); break;
                default: result.notImplemented();
            }
        } catch (Exception error) { result.error("native-action", error.getMessage(), null); }
    }
    private boolean canInstall() { return Build.VERSION.SDK_INT < 26 || getPackageManager().canRequestPackageInstalls(); }
    private void verifyAsync(String path, boolean install, MethodChannel.Result result) {
        background.execute(() -> {
            try {
                File apk = verifiedApk(path);
                runOnUiThread(() -> {
                    try {
                        if (!install) { result.success(null); return; }
                        if (!canInstall()) { result.success("permission-required"); return; }
                        Uri uri = FileProvider.getUriForFile(this, getPackageName() + ".files", apk);
                        startActivity(new Intent(Intent.ACTION_VIEW).setDataAndType(uri, "application/vnd.android.package-archive")
                            .addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION));
                        result.success("installing");
                    } catch (Exception error) { result.error("install-update", error.getMessage(), null); }
                });
            } catch (Exception error) { runOnUiThread(() -> result.error("verify-update", error.getMessage(), null)); }
        });
    }
    @SuppressWarnings("deprecation") private File verifiedApk(String path) throws Exception {
        if (path == null) throw new IllegalArgumentException("更新文件不存在");
        File apk = new File(path).getCanonicalFile(), directory = new File(getCacheDir(), "updates").getCanonicalFile();
        if (!directory.equals(apk.getParentFile()) || !apk.getName().endsWith(".apk") || !apk.isFile() || apk.length() == 0 || apk.length() > 160L * 1024 * 1024)
            throw new IllegalArgumentException("更新文件无效");
        PackageManager manager = getPackageManager();
        int flags = Build.VERSION.SDK_INT >= 28 ? PackageManager.GET_SIGNING_CERTIFICATES : PackageManager.GET_SIGNATURES;
        PackageInfo candidate = manager.getPackageArchiveInfo(apk.getPath(), flags), installed = manager.getPackageInfo(getPackageName(), flags);
        if (candidate == null || !getPackageName().equals(candidate.packageName)) throw new IllegalArgumentException("安装包不属于 AIRemote");
        long version = Build.VERSION.SDK_INT >= 28 ? candidate.getLongVersionCode() : candidate.versionCode;
        if (version <= BuildConfig.VERSION_CODE) throw new IllegalArgumentException("安装包版本没有更新");
        Signature[] a = Build.VERSION.SDK_INT >= 28 ? candidate.signingInfo.getApkContentsSigners() : candidate.signatures;
        Signature[] b = Build.VERSION.SDK_INT >= 28 ? installed.signingInfo.getApkContentsSigners() : installed.signatures;
        if (a == null || b == null || a.length != 1 || b.length != 1 || !MessageDigest.isEqual(a[0].toByteArray(), b[0].toByteArray()))
            throw new IllegalArgumentException("安装包签名与当前应用不一致，已停止安装");
        return apk;
    }
    private void enqueueDownload(MethodCall call) {
        String server = ServerAddress.normalize(call.argument("server")), address = call.argument("url"), cookie = call.argument("cookie");
        URI uri = URI.create(address);
        if (!ServerAddress.sameOrigin(address, server) || uri.getQuery() != null || uri.getFragment() != null || !uri.getPath().matches("/api/requests/[0-9a-f-]{36}/result"))
            throw new IllegalArgumentException("下载地址不属于当前服务器");
        String name = call.argument("name");
        if (name == null || name.isEmpty()) name = "AIRemote-file";
        name = name.replaceAll("[/\\\\\\x00-\\x1f]", "_");
        if (name.startsWith(".")) name = "file-" + name;
        if (name.length() > 180) name = name.substring(name.length() - 180);
        DownloadManager.Request request = new DownloadManager.Request(Uri.parse(address)).setTitle(name).setMimeType("application/octet-stream")
            .setNotificationVisibility(DownloadManager.Request.VISIBILITY_VISIBLE_NOTIFY_COMPLETED);
        if (cookie != null && !cookie.isEmpty()) request.addRequestHeader("Cookie", cookie);
        if (Build.VERSION.SDK_INT >= 29) request.setDestinationInExternalPublicDir(Environment.DIRECTORY_DOWNLOADS, name);
        else request.setDestinationInExternalFilesDir(this, Environment.DIRECTORY_DOWNLOADS, name);
        ((DownloadManager) getSystemService(DOWNLOAD_SERVICE)).enqueue(request);
    }
    @Override protected void onDestroy() { background.shutdown(); super.onDestroy(); }
}
