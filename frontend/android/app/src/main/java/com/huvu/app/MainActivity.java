package com.huvu.app;

import android.os.Bundle;
import com.getcapacitor.BridgeActivity;
import com.huvu.app.wake.WakeWordPlugin;

public class MainActivity extends BridgeActivity {
    @Override
    public void onCreate(Bundle savedInstanceState) {
        // The app's own plugins, which Capacitor doesn't find on its own.
        registerPlugin(WakeWordPlugin.class);
        super.onCreate(savedInstanceState);
    }
}
