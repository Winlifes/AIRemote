package fun.winlife.airemote;

import android.app.*;
import android.content.*;
import android.content.pm.*;
import android.graphics.Color;
import android.net.Uri;
import android.net.http.SslError;
import android.os.*;
import android.provider.Settings;
import android.view.*;
import android.webkit.*;
import android.widget.*;
import androidx.core.content.FileProvider;
import androidx.core.view.*;
import java.io.File;
import java.net.URI;
import java.util.Arrays;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

public class MainActivity extends Activity {
    private WebView web;
    private FrameLayout root;
    private SharedPreferences preferences;
    private String server;
    private File pendingInstall;
    private volatile boolean checkingUpdate;
    private final ExecutorService background = Executors.newSingleThreadExecutor();

    @Override public void onCreate(Bundle state) {
        super.onCreate(state);
        preferences = getSharedPreferences("airemote", MODE_PRIVATE);
        server = preferences.getString("server", "");
        root = new FrameLayout(this); root.setBackgroundColor(Color.rgb(250, 250, 250));
        setContentView(root);
        ViewCompat.setOnApplyWindowInsetsListener(root, (view, insets) -> {
            var bars = insets.getInsets(WindowInsetsCompat.Type.systemBars());
            view.setPadding(bars.left, bars.top, bars.right, bars.bottom);
            return insets;
        });
        WebView.setWebContentsDebuggingEnabled(BuildConfig.DEBUG);
        if (server.isEmpty()) showSetup(); else loadServer();
    }
    private int dp(int value) { return Math.round(value * getResources().getDisplayMetrics().density); }
    private void toast(String message) { runOnUiThread(() -> Toast.makeText(this, message, Toast.LENGTH_LONG).show()); }
    static String normalizeServer(String value) {
        return ServerAddress.normalize(value);
    }
    private boolean sameOrigin(String address) {
        try {
            return ServerAddress.sameOrigin(address, server);
        } catch (Exception ignored) { return false; }
    }
    private void showSetup() {
        if (web != null) { root.removeView(web); web.destroy(); web = null; }
        LinearLayout content = new LinearLayout(this); content.setOrientation(LinearLayout.VERTICAL);
        content.setGravity(Gravity.CENTER_VERTICAL); content.setPadding(dp(28), dp(24), dp(28), dp(24));
        TextView title = new TextView(this); title.setText("AIRemote"); title.setTextSize(32); title.setTextColor(Color.BLACK);
        TextView description = new TextView(this); description.setText("连接你的 Mac AI 客户端\n\n填写你部署的 AIRemote 服务器地址，账号和密码在连接后输入。");
        description.setTextSize(15); description.setPadding(0, dp(16), 0, dp(22));
        EditText address = new EditText(this); address.setSingleLine(true); address.setHint("https://remote.example.com"); address.setText(server);
        address.setInputType(android.text.InputType.TYPE_CLASS_TEXT | android.text.InputType.TYPE_TEXT_VARIATION_URI);
        Button connect = new Button(this); connect.setText("连接服务器"); connect.setOnClickListener(view -> {
            try { server = normalizeServer(address.getText().toString()); preferences.edit().putString("server", server).apply(); loadServer(); }
            catch (Exception error) { address.setError("请输入有效的 HTTPS 服务器根地址"); }
        });
        Button about = new Button(this); about.setText("应用设置与更新"); about.setOnClickListener(view -> showAppMenu());
        content.addView(title); content.addView(description); content.addView(address); content.addView(connect); content.addView(about);
        root.removeAllViews(); root.addView(content, new FrameLayout.LayoutParams(-1, -1));
    }
    @SuppressWarnings("SetJavaScriptEnabled") private void loadServer() {
        if (web != null) { root.removeView(web); web.destroy(); }
        web = new WebView(this); web.setBackgroundColor(Color.rgb(250, 250, 250));
        WebSettings settings = web.getSettings();
        settings.setJavaScriptEnabled(true); settings.setDomStorageEnabled(true);
        settings.setAllowFileAccess(false); settings.setAllowContentAccess(false);
        settings.setMixedContentMode(WebSettings.MIXED_CONTENT_NEVER_ALLOW);
        settings.setSafeBrowsingEnabled(true); settings.setTextZoom(100);
        CookieManager.getInstance().setAcceptCookie(true);
        CookieManager.getInstance().setAcceptThirdPartyCookies(web, false);
        web.addJavascriptInterface(new NativeActions(), "AIRemoteAndroid");
        web.setWebViewClient(new WebViewClient() {
            @Override public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
                String address = request.getUrl().toString();
                if (sameOrigin(address)) return false;
                if (request.isForMainFrame() && "https".equalsIgnoreCase(request.getUrl().getScheme())) {
                    try { startActivity(new Intent(Intent.ACTION_VIEW, request.getUrl())); } catch (Exception ignored) { toast("没有可打开此链接的浏览器"); }
                }
                return true;
            }
            @Override public void onReceivedSslError(WebView view, SslErrorHandler handler, SslError error) {
                handler.cancel(); showConnectionError("服务器 HTTPS 证书校验失败，请检查服务器配置。");
            }
            @Override public void onReceivedError(WebView view, WebResourceRequest request, WebResourceError error) {
                if (request.isForMainFrame()) showConnectionError("暂时无法连接服务器，请检查网络后重试。");
            }
            @Override public void onReceivedHttpError(WebView view, WebResourceRequest request, WebResourceResponse error) {
                if (request.isForMainFrame() && error.getStatusCode() >= 500) showConnectionError("服务器暂不可用，请稍后重试。");
            }
            @Override public void onPageFinished(WebView view, String url) { CookieManager.getInstance().flush(); }
        });
        web.setDownloadListener((url, userAgent, disposition, mime, length) -> {
            if (url.startsWith("blob:")) toast("请更新服务器网页，以启用 Android 文件下载。");
            else enqueueDownload(url, URLUtil.guessFileName(url, disposition, mime), mime);
        });
        root.removeAllViews(); root.addView(web, new FrameLayout.LayoutParams(-1, -1)); web.loadUrl(server);
        if (preferences.getBoolean("autoUpdate", true) && System.currentTimeMillis() - preferences.getLong("lastUpdateCheck", 0) > 6 * 60 * 60 * 1000L) checkUpdate(false);
    }
    private void showConnectionError(String message) {
        runOnUiThread(() -> new AlertDialog.Builder(this).setTitle("连接未完成").setMessage(message)
            .setPositiveButton("重试", (dialog, which) -> { if (web != null) web.reload(); })
            .setNegativeButton("更换服务器", (dialog, which) -> showSetup()).show());
    }
    public final class NativeActions {
        @JavascriptInterface public void openSettings() { runOnUiThread(() -> showAppMenu()); }
        @JavascriptInterface public void download(String address, String filename, String mime) {
            runOnUiThread(() -> { if (web != null && sameOrigin(web.getUrl())) enqueueDownload(address, filename, mime); });
        }
    }
    private void enqueueDownload(String address, String name, String mime) {
        try {
            URI uri = URI.create(address);
            if (!sameOrigin(address) || !uri.getPath().matches("/api/requests/[0-9a-f-]{36}/result") || uri.getQuery() != null)
                throw new IllegalArgumentException("下载必须来自当前服务器的授权文件请求");
            String filename = name.replaceAll("[\\\\/:*?\"<>|\\p{Cntrl}]", "_");
            if (filename.trim().isEmpty()) filename = "AIRemote-file";
            if (filename.startsWith(".")) filename = "file-" + filename;
            filename = filename.substring(0, Math.min(filename.length(), 120));
            DownloadManager.Request request = new DownloadManager.Request(Uri.parse(address));
            String cookie = CookieManager.getInstance().getCookie(address);
            if (cookie != null) request.addRequestHeader("Cookie", cookie);
            request.setTitle(filename); request.setDescription("来自 AIRemote 当前对话");
            request.setNotificationVisibility(DownloadManager.Request.VISIBILITY_VISIBLE_NOTIFY_COMPLETED);
            if (mime != null && mime.matches("[a-zA-Z0-9.+_-]+/[a-zA-Z0-9.+_-]+(?:;.*)?")) request.setMimeType(mime.split(";", 2)[0]);
            if (Build.VERSION.SDK_INT >= 29) request.setDestinationInExternalPublicDir(Environment.DIRECTORY_DOWNLOADS, "AIRemote/" + filename);
            else request.setDestinationInExternalFilesDir(this, Environment.DIRECTORY_DOWNLOADS, filename);
            ((DownloadManager) getSystemService(DOWNLOAD_SERVICE)).enqueue(request);
            toast("文件已交给系统下载，可在下载通知中打开。");
        } catch (Exception error) { toast("下载未开始：" + error.getMessage()); }
    }
    private void showAppMenu() {
        boolean auto = preferences.getBoolean("autoUpdate", true);
        String[] labels = {"服务器地址", "检查 GitHub 更新", auto ? "关闭自动检查更新" : "开启自动检查更新", "在浏览器打开", "GitHub 开源仓库"};
        new AlertDialog.Builder(this).setTitle("AIRemote " + BuildConfig.VERSION_NAME).setItems(labels, (dialog, choice) -> {
            if (choice == 0) showSetup();
            else if (choice == 1) checkUpdate(true);
            else if (choice == 2) { preferences.edit().putBoolean("autoUpdate", !auto).apply(); toast(auto ? "已关闭自动检查" : "已开启自动检查"); }
            else {
                String address = choice == 3 ? server : "https://github.com/" + BuildConfig.RELEASE_REPO;
                if (!address.isEmpty()) try { startActivity(new Intent(Intent.ACTION_VIEW, Uri.parse(address))); } catch (Exception ignored) { toast("没有可用的浏览器"); }
            }
        }).setNegativeButton("关闭", null).show();
    }
    private void checkUpdate(boolean manual) {
        if (checkingUpdate) { if (manual) toast("正在检查更新…"); return; }
        checkingUpdate = true; if (manual) toast("正在检查 GitHub Releases…");
        background.execute(() -> {
            try {
                ReleaseClient.Release release = ReleaseClient.latest();
                preferences.edit().putLong("lastUpdateCheck", System.currentTimeMillis()).apply();
                if (ReleaseVersion.code(release.version()) <= BuildConfig.VERSION_CODE) { if (manual) toast("当前已是最新版本 " + BuildConfig.VERSION_NAME); return; }
                runOnUiThread(() -> new AlertDialog.Builder(this).setTitle("发现新版本 " + release.version())
                    .setMessage("下载校验通过后，由 Android 系统确认安装。你的服务器设置与登录数据会保留。")
                    .setPositiveButton("下载更新", (dialog, which) -> downloadUpdate(release)).setNegativeButton("稍后", null).show());
            } catch (Exception error) { if (manual) toast("暂时无法检查更新：" + error.getMessage()); }
            finally { checkingUpdate = false; }
        });
    }
    private void downloadUpdate(ReleaseClient.Release release) {
        toast("正在从 GitHub 下载更新…");
        background.execute(() -> {
            try {
                File apk = ReleaseClient.download(release, new File(getCacheDir(), "updates")); verifyApk(apk);
                pendingInstall = apk; runOnUiThread(() -> installPending());
            } catch (Exception error) { toast("更新未安装：" + error.getMessage()); }
        });
    }
    @SuppressWarnings("deprecation") private void verifyApk(File apk) throws Exception {
        PackageManager manager = getPackageManager();
        int flags = Build.VERSION.SDK_INT >= 28 ? PackageManager.GET_SIGNING_CERTIFICATES : PackageManager.GET_SIGNATURES;
        PackageInfo candidate = manager.getPackageArchiveInfo(apk.getAbsolutePath(), flags);
        PackageInfo installed = manager.getPackageInfo(getPackageName(), flags);
        if (candidate == null || !getPackageName().equals(candidate.packageName)) throw new SecurityException("安装包标识不匹配");
        long candidateVersion = Build.VERSION.SDK_INT >= 28 ? candidate.getLongVersionCode() : candidate.versionCode;
        if (candidateVersion <= BuildConfig.VERSION_CODE) throw new SecurityException("不允许安装旧版本");
        Signature[] a = Build.VERSION.SDK_INT >= 28 ? candidate.signingInfo.getApkContentsSigners() : candidate.signatures;
        Signature[] b = Build.VERSION.SDK_INT >= 28 ? installed.signingInfo.getApkContentsSigners() : installed.signatures;
        if (a == null || b == null || a.length != 1 || b.length != 1 || !Arrays.equals(a[0].toByteArray(), b[0].toByteArray()))
            throw new SecurityException("安装包签名与当前应用不一致");
    }
    private void installPending() {
        if (pendingInstall == null || !pendingInstall.isFile()) return;
        if (!getPackageManager().canRequestPackageInstalls()) {
            new AlertDialog.Builder(this).setTitle("允许安装应用更新")
                .setMessage("请在系统设置中允许 AIRemote 安装更新，返回后继续。")
                .setPositiveButton("打开系统设置", (dialog, which) -> startActivity(new Intent(Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES, Uri.parse("package:" + getPackageName()))))
                .setNegativeButton("稍后", null).show(); return;
        }
        File apk = pendingInstall; pendingInstall = null;
        Uri uri = FileProvider.getUriForFile(this, getPackageName() + ".files", apk);
        Intent intent = new Intent(Intent.ACTION_VIEW).setDataAndType(uri, "application/vnd.android.package-archive").addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION);
        try { startActivity(intent); } catch (Exception error) { toast("请使用系统文件管理器安装已下载的更新。"); }
    }
    @Override protected void onResume() { super.onResume(); if (pendingInstall != null && getPackageManager().canRequestPackageInstalls()) installPending(); }
    @Override protected void onPause() { if (web != null) CookieManager.getInstance().flush(); super.onPause(); }
    @Override protected void onDestroy() { background.shutdown(); if (web != null) web.destroy(); super.onDestroy(); }
    @SuppressWarnings("deprecation") @Override public void onBackPressed() {
        if (web == null || web.getParent() == null) { if (!server.isEmpty()) loadServer(); else super.onBackPressed(); return; }
        web.evaluateJavascript("(()=>{const close=document.querySelector('.app.drawer-open .drawer-close');if(close){close.click();return true;}return false;})()", result -> {
            if ("true".equals(result)) return;
            if (web.canGoBack()) web.goBack(); else MainActivity.super.onBackPressed();
        });
    }
}
