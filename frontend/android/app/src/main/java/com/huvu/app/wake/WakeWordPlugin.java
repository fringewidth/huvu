package com.huvu.app.wake;

import android.Manifest;
import android.content.Context;
import android.content.Intent;
import android.net.Uri;
import android.provider.Settings;
import com.getcapacitor.JSObject;
import com.getcapacitor.PermissionState;
import com.getcapacitor.Plugin;
import com.getcapacitor.PluginCall;
import com.getcapacitor.PluginMethod;
import com.getcapacitor.annotation.CapacitorPlugin;
import com.getcapacitor.annotation.Permission;
import com.getcapacitor.annotation.PermissionCallback;
import java.io.IOException;
import java.util.Arrays;

// The web app's side of the wake word (voice/wakeWord.ts). Whether it is
// on is kept here, so it stays on across restarts and live updates. It
// starts WakeService each time huvu comes to the front, the only time
// Android lets a microphone service start, and tells the web app when the
// service heard "hey huvu".
@CapacitorPlugin(name = "WakeWord", permissions = { @Permission(alias = "microphone", strings = { Manifest.permission.RECORD_AUDIO }) })
public class WakeWordPlugin extends Plugin {
    static final String HEARD = "com.huvu.app.wake.HEARD";
    private static final String PREFS = "huvu.wake";

    private static volatile WakeWordPlugin loaded;
    private static volatile boolean front;

    static boolean inFront() {
        return front;
    }

    static void heard() {
        WakeWordPlugin plugin = loaded;
        if (plugin != null) plugin.notifyListeners("heard", new JSObject(), true);
    }

    static void setOn(Context context, boolean on) {
        context.getSharedPreferences(PREFS, Context.MODE_PRIVATE).edit().putBoolean("on", on).apply();
    }

    private static boolean isOn(Context context) {
        return context.getSharedPreferences(PREFS, Context.MODE_PRIVATE).getBoolean("on", false);
    }

    @Override
    public void load() {
        loaded = this;
        // huvu was opened by the service, and the activity was gone.
        heardIn(getActivity().getIntent());
    }

    @Override
    protected void handleOnNewIntent(Intent intent) {
        heardIn(intent);
    }

    @Override
    protected void handleOnResume() {
        front = true;
        ensure();
    }

    @Override
    protected void handleOnPause() {
        front = false;
    }

    private void heardIn(Intent intent) {
        if (intent == null || !intent.getBooleanExtra(HEARD, false)) return;
        // Once only, so the activity coming back after a rotation doesn't
        // hear it again.
        intent.removeExtra(HEARD);
        heard();
    }

    // On, allowed the mic, and a model in this build: listening.
    private void ensure() {
        if (isOn(getContext()) && micAllowed() && hasModel()) WakeService.start(getContext());
    }

    private boolean micAllowed() {
        return getPermissionState("microphone") == PermissionState.GRANTED;
    }

    private boolean hasModel() {
        try {
            return Arrays.asList(getContext().getAssets().list(WakeService.MODELS)).contains(WakeService.WAKE_MODEL);
        } catch (IOException e) {
            return false;
        }
    }

    private JSObject state() {
        JSObject state = new JSObject();
        state.put("on", isOn(getContext()) && micAllowed());
        state.put("overlay", Settings.canDrawOverlays(getContext()));
        return state;
    }

    @PluginMethod
    public void state(PluginCall call) {
        call.resolve(state());
    }

    @PluginMethod
    public void turnOn(PluginCall call) {
        if (!hasModel()) {
            call.reject("this build has no hey huvu model");
            return;
        }
        if (!micAllowed()) {
            requestPermissionForAlias("microphone", call, "afterMic");
            return;
        }
        setOn(getContext(), true);
        ensure();
        call.resolve(state());
    }

    @PermissionCallback
    private void afterMic(PluginCall call) {
        if (!micAllowed()) {
            call.reject("allow the mic to hear hey huvu");
            return;
        }
        turnOn(call);
    }

    @PluginMethod
    public void turnOff(PluginCall call) {
        setOn(getContext(), false);
        WakeService.stop(getContext());
        call.resolve(state());
    }

    // Opens Android's "display over other apps" for huvu, the only way a
    // background app may bring itself to the front.
    @PluginMethod
    public void allowOverlay(PluginCall call) {
        Intent settings = new Intent(Settings.ACTION_MANAGE_OVERLAY_PERMISSION, Uri.parse("package:" + getContext().getPackageName()));
        getActivity().startActivity(settings);
        call.resolve();
    }

    // Resolves once the service has let go of the mic, or taken it back.
    @PluginMethod
    public void hold(PluginCall call) {
        WakeService.hold(call.getBoolean("held", false));
        call.resolve();
    }
}
