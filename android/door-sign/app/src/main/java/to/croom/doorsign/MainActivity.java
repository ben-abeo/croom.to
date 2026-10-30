package to.croom.doorsign;

import android.annotation.SuppressLint;
import android.app.Activity;
import android.content.Intent;
import android.graphics.Color;
import android.graphics.Bitmap;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.util.Log;
import android.view.Gravity;
import android.view.MotionEvent;
import android.view.View;
import android.view.WindowInsets;
import android.view.WindowInsetsController;
import android.view.WindowManager;
import android.webkit.JavascriptInterface;
import android.webkit.RenderProcessGoneDetail;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebResourceResponse;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.FrameLayout;
import android.widget.TextView;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

/**
 * The door sign, full screen: the room's /sign page, and the light bar round the panel in the colour of the state the page shows
 * (window.CrystalPanel.setLight). Holding the top-left corner for five seconds opens setup, behind the PIN. When the app is the
 * panel's device owner the sign runs in Android's lock-task kiosk; the check-in with the room (DoorSignApp) runs whatever the sign shows.
 */
public class MainActivity extends Activity {

    private static final String TAG = "DoorSign";
    private static final long RETRY_MS = 10_000;
    private static final long CORNER_HOLD_MS = 5_000;
    private static final int CORNER_DP = 96;

    // The panel maker's SystemUI hides and shows its system bars on these (the panel's BroadcastInterFace sends them too);
    // another tablet has no receiver for them, and nothing happens.
    static final String SYSTEM_BARS_HIDE = "elc.view.hide";
    static final String SYSTEM_BARS_SHOW = "elc.view.show";

    private final Handler main = new Handler(Looper.getMainLooper());
    private final ExecutorService lightThread = Executors.newSingleThreadExecutor();
    private final Runnable retry = () -> load(this.signUrl);
    private final Runnable openSetup = () -> startActivity(new Intent(this, SetupActivity.class));

    private SignSettings settings;
    private LightBar lights;
    private WebView web;
    private TextView error;

    private String signUrl;
    private boolean failed;
    private volatile String pageUrl = "";      // the page the WebView is on, for the bridge, which runs off the UI thread
    private volatile String lastState;         // the state the light bar shows, to put back when the sign comes to the front again

    @Override protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
        settings = new SignSettings(this);
        lights = new LightBar(new LedDevice());
        setContentView(layout());
    }

    @Override protected void onResume() {
        super.onResume();
        hideSystemBars();
        main.postDelayed(this::hideSystemBars, 2_000);   // at power-on the sign starts before the system bars are ready to be hidden
        String url = settings.url();
        if (url == null) {
            startActivity(new Intent(this, SetupActivity.class));
            return;
        }
        Kiosk.enter(this);     // as the panel's device owner: Android's lock-task kiosk (Setup's Exit to Android ends it)
        if (!url.equals(signUrl)) {
            load(url);
        } else if (lastState != null) {
            showLight(lastState);     // setup's light test, or Exit to Android, left the bar in another colour
        }
    }

    @Override public void onWindowFocusChanged(boolean hasFocus) {
        super.onWindowFocusChanged(hasFocus);
        if (hasFocus) hideSystemBars();
    }

    @SuppressLint("MissingSuperCall")
    @Override public void onBackPressed() {
        // A door sign has nowhere to go back to: setup, behind the corner and the PIN, is the way out.
    }

    @Override protected void onDestroy() {
        main.removeCallbacksAndMessages(null);
        web.destroy();
        lightThread.shutdown();
        super.onDestroy();
    }

    private void load(String url) {
        main.removeCallbacks(retry);
        signUrl = url;
        failed = false;
        web.loadUrl(url);
    }

    private void showLight(String state) {
        Log.i(TAG, "Light bar: " + state);
        lastState = state;
        lightThread.execute(() -> lights.show(state));
    }

    /** The page did not load: say so, put the light bar out (the sign is not there to say the room is free) and try again shortly. */
    private void showError(String why) {
        failed = true;
        error.setText("Can't reach Crystal Meet at " + signUrl + "\n\n" + why + "\n\nTrying again every 10 seconds.");
        error.setVisibility(View.VISIBLE);
        web.setVisibility(View.INVISIBLE);
        showLight("offline");
        main.removeCallbacks(retry);
        main.postDelayed(retry, RETRY_MS);
    }

    /** What the page calls, as window.CrystalPanel. Only the sign's own server is listened to. */
    private final class Panel {
        @JavascriptInterface public void setLight(String state) {
            if (signUrl != null && SignAddress.sameServer(signUrl, pageUrl)) showLight(state);
        }
    }

    @SuppressLint({"SetJavaScriptEnabled", "ClickableViewAccessibility"})
    private View layout() {
        FrameLayout root = new FrameLayout(this);
        root.setBackgroundColor(Color.rgb(0, 22, 54));   // the sign's navy, while the page loads

        web = new WebView(this);
        WebSettings ws = web.getSettings();
        ws.setJavaScriptEnabled(true);
        ws.setDomStorageEnabled(true);
        ws.setMediaPlaybackRequiresUserGesture(false);
        web.setBackgroundColor(Color.rgb(0, 22, 54));
        web.addJavascriptInterface(new Panel(), "CrystalPanel");
        web.setWebViewClient(new SignClient());
        root.addView(web, new FrameLayout.LayoutParams(FrameLayout.LayoutParams.MATCH_PARENT, FrameLayout.LayoutParams.MATCH_PARENT));

        error = new TextView(this);
        error.setTextColor(Color.WHITE);
        error.setTextSize(22);
        error.setGravity(Gravity.CENTER);
        error.setPadding(dp(48), dp(48), dp(48), dp(48));
        error.setVisibility(View.GONE);
        root.addView(error, new FrameLayout.LayoutParams(FrameLayout.LayoutParams.MATCH_PARENT, FrameLayout.LayoutParams.MATCH_PARENT));

        View corner = new View(this);
        corner.setOnTouchListener((v, event) -> {
            int action = event.getActionMasked();
            if (action == MotionEvent.ACTION_DOWN) main.postDelayed(openSetup, CORNER_HOLD_MS);
            if (action == MotionEvent.ACTION_UP || action == MotionEvent.ACTION_CANCEL) main.removeCallbacks(openSetup);
            return true;
        });
        root.addView(corner, new FrameLayout.LayoutParams(dp(CORNER_DP), dp(CORNER_DP), Gravity.TOP | Gravity.START));
        return root;
    }

    private final class SignClient extends WebViewClient {
        @Override public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
            return !SignAddress.sameServer(signUrl, request.getUrl().toString());   // the sign stays on its server
        }

        @Override public void onPageStarted(WebView view, String url, Bitmap favicon) {
            pageUrl = url;
        }

        @Override public void onPageFinished(WebView view, String url) {
            hideSystemBars();
            if (failed) return;
            error.setVisibility(View.GONE);
            web.setVisibility(View.VISIBLE);
        }

        @Override public void onReceivedError(WebView view, WebResourceRequest request, WebResourceError err) {
            if (request.isForMainFrame()) showError(String.valueOf(err.getDescription()));
        }

        @Override public void onReceivedHttpError(WebView view, WebResourceRequest request, WebResourceResponse response) {
            if (request.isForMainFrame()) showError("The server answered " + response.getStatusCode() + " " + response.getReasonPhrase());
        }

        @Override public boolean onRenderProcessGone(WebView view, RenderProcessGoneDetail detail) {
            showLight("offline");
            recreate();     // a new WebView, and the sign loads again
            return true;
        }
    }

    @SuppressWarnings("deprecation")
    private void hideSystemBars() {
        sendBroadcast(new Intent(SYSTEM_BARS_HIDE));   // the SMT1019's SystemUI keeps its status bar up after a restart until told this
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
            getWindow().setDecorFitsSystemWindows(false);
            WindowInsetsController controller = getWindow().getInsetsController();
            if (controller != null) {
                controller.hide(WindowInsets.Type.systemBars());
                controller.setSystemBarsBehavior(WindowInsetsController.BEHAVIOR_SHOW_TRANSIENT_BARS_BY_SWIPE);
            }
        } else {
            getWindow().getDecorView().setSystemUiVisibility(View.SYSTEM_UI_FLAG_IMMERSIVE_STICKY | View.SYSTEM_UI_FLAG_FULLSCREEN
                    | View.SYSTEM_UI_FLAG_HIDE_NAVIGATION | View.SYSTEM_UI_FLAG_LAYOUT_STABLE
                    | View.SYSTEM_UI_FLAG_LAYOUT_HIDE_NAVIGATION | View.SYSTEM_UI_FLAG_LAYOUT_FULLSCREEN);
        }
    }

    private int dp(int value) {
        return Math.round(value * getResources().getDisplayMetrics().density);
    }
}
