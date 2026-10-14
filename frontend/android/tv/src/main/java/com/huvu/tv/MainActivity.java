package com.huvu.tv;

import android.app.Activity;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;

// The TV's page, in a full-screen WebView. The page is the frontend built
// for the firetv target, served by the host, so a frontend change reaches
// the TV on its next launch with no new APK. The host's address comes from
// HUVU_PUBLIC_URL at build time (tv/build.gradle).
public class MainActivity extends Activity {
    private static final String PAGE = BuildConfig.PAGE;
    // How long to wait before loading the page again after it failed.
    private static final long RETRY_MS = 5000;

    private final Handler handler = new Handler(Looper.getMainLooper());
    private WebView web;
    private String page;

    @Override
    protected void onCreate(Bundle state) {
        super.onCreate(state);
        WebView.setWebContentsDebuggingEnabled(BuildConfig.DEBUG);
        page = page();

        web = new WebView(this);
        web.setBackgroundColor(getResources().getColor(R.color.paper));
        WebSettings settings = web.getSettings();
        settings.setJavaScriptEnabled(true);
        settings.setDomStorageEnabled(true);
        // huvu says its answers without waiting for a press.
        settings.setMediaPlaybackRequiresUserGesture(false);
        web.setWebViewClient(new WebViewClient() {
            // Called for the page itself only, on every Android version.
            // The host or the network is down: show paper, not an error
            // page, and try again.
            @Override
            public void onReceivedError(WebView view, int code, String description, String url) {
                view.loadUrl("about:blank");
                handler.removeCallbacksAndMessages(null);
                handler.postDelayed(() -> view.loadUrl(page), RETRY_MS);
            }
        });
        setContentView(web);
        web.requestFocus();
        web.loadUrl(page);
    }

    // A debug build loads the page from elsewhere when started with a url,
    // such as the Vite dev server on the dev machine (Fire OS 5's adb can't
    // reverse ports over Wi-Fi, so it is the machine's own address):
    //   adb shell am start -S -n com.huvu.tv/.MainActivity -e url http://<mac-ip>:5174/
    private String page() {
        String url = BuildConfig.DEBUG ? getIntent().getStringExtra("url") : null;
        if (url == null) url = PAGE;
        return url;
    }

    // Back undoes on the page first (window.huvuBack in Stage.tsx), and
    // leaves the app once there is nothing left to undo, or when the page
    // never loaded.
    @Override
    public void onBackPressed() {
        web.evaluateJavascript("typeof huvuBack === 'function' && huvuBack() === true", (undid) -> {
            if (!"true".equals(undid)) finish();
        });
    }

    @Override
    protected void onDestroy() {
        handler.removeCallbacksAndMessages(null);
        web.destroy();
        super.onDestroy();
    }
}
