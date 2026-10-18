package dev.kouzz.console;

import android.app.Activity;
import android.content.ActivityNotFoundException;
import android.content.Intent;
import android.graphics.Color;
import android.net.Uri;
import android.os.Bundle;
import android.webkit.JavascriptInterface;
import android.webkit.WebResourceRequest;
import android.webkit.WebResourceResponse;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.view.ViewGroup;
import android.widget.FrameLayout;
import org.json.JSONObject;
import java.io.ByteArrayInputStream;
import java.util.HashMap;
import java.util.Map;
import java.util.concurrent.ArrayBlockingQueue;
import java.util.concurrent.ThreadPoolExecutor;
import java.util.concurrent.TimeUnit;

public final class MainActivity extends Activity {
    private static final String ENTRY = "file:///android_asset/client/index.html";
    private WebView web;
    private NativeApi api;
    private volatile boolean destroyed;
    private final ThreadPoolExecutor requests = new ThreadPoolExecutor(2, 2, 0,
            TimeUnit.SECONDS, new ArrayBlockingQueue<>(32));

    @Override public void onCreate(Bundle state) {
        super.onCreate(state);
        api = new NativeApi(getApplicationContext());
        web = new WebView(this);
        web.setBackgroundColor(Color.rgb(14, 17, 23));
        FrameLayout frame = new FrameLayout(this);
        frame.addView(web, new FrameLayout.LayoutParams(-1, -1));
        // targetSdk 35 enforces edge-to-edge; keep frontend out of system bars.
        frame.setOnApplyWindowInsetsListener((view, insets) -> {
            view.setPadding(insets.getSystemWindowInsetLeft(), insets.getSystemWindowInsetTop(),
                    insets.getSystemWindowInsetRight(), insets.getSystemWindowInsetBottom());
            return insets.consumeSystemWindowInsets();
        });
        setContentView(frame);
        WebSettings s = web.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setAllowFileAccess(true);
        s.setAllowContentAccess(false);
        // Local ES modules need file-origin access. The interceptor admits only our assets.
        s.setAllowFileAccessFromFileURLs(true);
        s.setAllowUniversalAccessFromFileURLs(false);
        s.setMixedContentMode(WebSettings.MIXED_CONTENT_NEVER_ALLOW);
        s.setJavaScriptCanOpenWindowsAutomatically(false);
        s.setSupportMultipleWindows(false);
        WebView.setWebContentsDebuggingEnabled(false);
        web.addJavascriptInterface(new Bridge(), "Native");
        web.setWebViewClient(new WebViewClient() {
            @Override public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
                if (isAsset(request.getUrl()) && request.isForMainFrame()) return false;
                if (request.isForMainFrame() && request.hasGesture()) openBrowser(request.getUrl());
                return true;
            }
            @Override public boolean shouldOverrideUrlLoading(WebView view, String url) {
                Uri uri = Uri.parse(url);
                if (isAsset(uri)) return false;
                openBrowser(uri);
                return true;
            }
            @Override public WebResourceResponse shouldInterceptRequest(WebView view, WebResourceRequest request) {
                Uri uri = request.getUrl();
                if (isAsset(uri)) {
                    try {
                        String path = uri.getPath().substring("/android_asset/".length());
                        String mime = java.net.URLConnection.guessContentTypeFromName(path);
                        if (path.endsWith(".js")) mime = "application/javascript";
                        if (path.endsWith(".css")) mime = "text/css";
                        if (path.endsWith(".svg")) mime = "image/svg+xml";
                        if (mime == null) mime = "application/octet-stream";
                        Map<String, String> headers = new HashMap<>();
                        headers.put("Access-Control-Allow-Origin", "*");
                        headers.put("Content-Security-Policy", "default-src 'none'; script-src 'self' file: 'unsafe-inline'; style-src 'self' file: 'unsafe-inline'; img-src 'self' file: https: data:; font-src 'self' file: data:; connect-src 'none'; frame-src 'none'; child-src 'none'; object-src 'none'; base-uri 'none'; form-action 'none'");
                        headers.put("X-Content-Type-Options", "nosniff");
                        return new WebResourceResponse(mime, "UTF-8", 200, "OK", headers, getAssets().open(path));
                    } catch (java.io.IOException ignored) {
                        return new WebResourceResponse("text/plain", "UTF-8", new ByteArrayInputStream(new byte[0]));
                    }
                }
                // Images may be remote; remote HTML/scripts/frames cannot reach the bridge.
                String destination = request.getRequestHeaders().get("Sec-Fetch-Dest");
                if (destination == null) destination = request.getRequestHeaders().get("sec-fetch-dest");
                String host = uri.getHost();
                String path = uri.getPath();
                boolean bilibiliImage = host != null && host.endsWith(".hdslb.com") && path != null &&
                        path.toLowerCase(java.util.Locale.ROOT).matches(".*\\.(jpe?g|png|webp|gif|avif)(@.*)?$");
                if (!request.isForMainFrame() && "https".equals(uri.getScheme()) &&
                        ("image".equals(destination) || (destination == null && bilibiliImage))) return null;
                return new WebResourceResponse("text/plain", "UTF-8", 403, "Blocked",
                        java.util.Collections.emptyMap(), new ByteArrayInputStream(new byte[0]));
            }
        });
        web.loadUrl(ENTRY);
    }

    private static boolean isAsset(Uri uri) {
        if (!"file".equals(uri.getScheme()) || (uri.getAuthority() != null && !uri.getAuthority().isEmpty())) return false;
        String path = uri.getPath();
        return path != null && path.startsWith("/android_asset/client/") &&
                !path.contains("..") && !path.contains("\\");
    }

    private void openBrowser(Uri uri) {
        if (!("https".equals(uri.getScheme()) || "http".equals(uri.getScheme())) ||
                uri.getHost() == null || uri.getUserInfo() != null) return;
        try {
            startActivity(new Intent(Intent.ACTION_VIEW, uri).addCategory(Intent.CATEGORY_BROWSABLE));
        } catch (ActivityNotFoundException ignored) { }
    }

    public final class Bridge {
        @JavascriptInterface public void request(String method, String path, String body, String callbackId) {
            if (destroyed || callbackId == null || callbackId.length() > 200) return;
            if (body != null && body.length() > 65536) {
                resolve(callbackId, error("请求内容过大"));
                return;
            }
            try {
                requests.execute(() -> {
                    JSONObject result;
                    try {
                        result = new JSONObject().put("ok", true).put("data", api.handle(method, path, body));
                    } catch (Exception e) {
                        result = error(e.getMessage() == null ? "原生请求失败" : e.getMessage());
                    }
                    resolve(callbackId, result);
                });
            } catch (java.util.concurrent.RejectedExecutionException e) {
                resolve(callbackId, error("请求过多，请稍后重试"));
            }
        }
    }

    private static JSONObject error(String message) {
        JSONObject value = new JSONObject();
        try { value.put("ok", false).put("error", message); } catch (Exception ignored) { }
        return value;
    }

    private void resolve(String id, JSONObject value) {
        String script = "window.__nativeResolve && window.__nativeResolve(" + JSONObject.quote(id) + "," + value + ");";
        runOnUiThread(() -> { if (!destroyed) web.evaluateJavascript(script, null); });
    }

    @Override public void onBackPressed() {
        if (web.canGoBack()) web.goBack(); else super.onBackPressed();
    }

    @Override protected void onDestroy() {
        destroyed = true;
        requests.shutdownNow();
        api.close();
        web.removeJavascriptInterface("Native");
        web.stopLoading();
        ((ViewGroup) web.getParent()).removeView(web);
        web.destroy();
        super.onDestroy();
    }
}
