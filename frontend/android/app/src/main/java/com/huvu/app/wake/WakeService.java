package com.huvu.app.wake;

import android.app.KeyguardManager;
import android.app.Notification;
import android.app.PendingIntent;
import android.app.Service;
import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.content.IntentFilter;
import android.content.pm.ServiceInfo;
import android.media.AudioFormat;
import android.media.AudioRecord;
import android.media.MediaRecorder;
import android.os.Build;
import android.os.Handler;
import android.os.IBinder;
import android.os.Looper;
import android.os.PowerManager;
import android.os.SystemClock;
import android.provider.Settings;
import android.util.Log;
import androidx.core.app.NotificationChannelCompat;
import androidx.core.app.NotificationCompat;
import androidx.core.app.NotificationManagerCompat;
import androidx.core.app.ServiceCompat;
import androidx.core.content.ContextCompat;
import com.huvu.app.MainActivity;
import com.huvu.app.R;
import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.InputStream;

// Listens for "hey huvu" while the phone is on and unlocked, whatever app
// is in front, and opens huvu in voice mode when it hears it. It stops
// listening while the screen is off or locked, since anyone at the lock
// screen could talk to huvu as its owner, and while huvu's own voice mode
// has the mic.
//
// A foreground service of type microphone: Android lets it record only
// while it shows a notification, and only if it started while huvu was in
// front, so WakeWordPlugin starts it each time huvu comes to the front.
// After a reboot it waits for huvu to be opened once.
public class WakeService extends Service {
    private static final String TAG = "huvu.wake";
    private static final String CHANNEL = "huvu.wake";
    private static final int NOTIFICATION = 7301;
    private static final String TURN_OFF = "com.huvu.app.wake.TURN_OFF";
    private static final int RATE = 16000;
    // The score openWakeWord counts as heard.
    private static final float THRESHOLD = 0.5f;
    // The phrase is still ending for a moment after it is heard, so the
    // same "hey huvu" doesn't count twice.
    private static final long DEAF_MS = 2000;

    static final String MODELS = "wakeword";
    static final String WAKE_MODEL = "hey_huvu.onnx";

    private static volatile WakeService running;

    private final Handler main = new Handler(Looper.getMainLooper());
    private WakeDetector detector;
    private Thread loop;
    private volatile boolean listening;
    // Why it isn't listening: the screen is off or locked, or huvu's voice
    // mode holds the mic. Voice mode can take the mic before the service
    // starts, so that one outlives the service.
    private boolean screenReady;
    private static volatile boolean held;

    private final BroadcastReceiver screen = new BroadcastReceiver() {
        @Override
        public void onReceive(Context context, Intent intent) {
            onScreen();
        }
    };

    static void start(Context context) {
        ContextCompat.startForegroundService(context, new Intent(context, WakeService.class));
    }

    static void stop(Context context) {
        context.stopService(new Intent(context, WakeService.class));
    }

    // huvu's voice mode takes the mic, or gives it back. Returns once the
    // listener has let go, so the WebView's recording never gets silence.
    static void hold(boolean held) {
        WakeService.held = held;
        WakeService service = running;
        if (service != null) service.heldChanged();
    }

    private synchronized void heldChanged() {
        update();
    }

    @Override
    public void onCreate() {
        super.onCreate();
        try {
            detector = new WakeDetector(asset(MODELS + "/melspectrogram.onnx"), asset(MODELS + "/embedding_model.onnx"), asset(MODELS + "/" + WAKE_MODEL));
        } catch (Exception e) {
            Log.e(TAG, "couldn't load the wake word models", e);
        }
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        if (intent != null && TURN_OFF.equals(intent.getAction())) {
            WakeWordPlugin.setOn(this, false);
            stopSelf();
            return START_NOT_STICKY;
        }
        try {
            ServiceCompat.startForeground(this, NOTIFICATION, notification(), ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE);
        } catch (RuntimeException e) {
            // Started from the background, or without the mic allowed.
            Log.w(TAG, "couldn't start listening", e);
            stopSelf();
            return START_NOT_STICKY;
        }
        if (detector == null) {
            stopSelf();
            return START_NOT_STICKY;
        }
        if (running == null) {
            running = this;
            IntentFilter filter = new IntentFilter(Intent.ACTION_SCREEN_OFF);
            filter.addAction(Intent.ACTION_SCREEN_ON);
            filter.addAction(Intent.ACTION_USER_PRESENT);
            registerReceiver(screen, filter);
            onScreen();
        }
        // Android would start it again from the background, where it may not
        // record. huvu starts it the next time it comes to the front.
        return START_NOT_STICKY;
    }

    @Override
    public void onDestroy() {
        if (running == this) {
            running = null;
            unregisterReceiver(screen);
        }
        synchronized (this) {
            listening = false;
            joinLoop();
        }
        if (detector != null) {
            try {
                detector.close();
            } catch (Exception e) {
                Log.w(TAG, "couldn't close the wake word models", e);
            }
        }
        super.onDestroy();
    }

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }

    private boolean screenReady() {
        PowerManager power = getSystemService(PowerManager.class);
        KeyguardManager keyguard = getSystemService(KeyguardManager.class);
        return power.isInteractive() && !keyguard.isKeyguardLocked();
    }

    private synchronized void onScreen() {
        screenReady = screenReady();
        update();
    }

    // Starts or stops the listener to match. Callers hold the lock.
    private void update() {
        boolean should = screenReady && !held;
        if (should && loop == null) {
            listening = true;
            loop = new Thread(this::listen, "huvu.wake");
            loop.start();
        } else if (!should && loop != null) {
            listening = false;
            joinLoop();
        }
    }

    private void joinLoop() {
        Thread stopping = loop;
        loop = null;
        if (stopping == null) return;
        try {
            stopping.join();
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
        }
    }

    private void listen() {
        int min = AudioRecord.getMinBufferSize(RATE, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT);
        AudioRecord recorder;
        try {
            recorder = new AudioRecord(MediaRecorder.AudioSource.VOICE_RECOGNITION, RATE, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT, Math.max(min, WakeDetector.FRAME * 2 * 4));
        } catch (SecurityException e) {
            Log.w(TAG, "the mic isn't allowed", e);
            return;
        }
        if (recorder.getState() != AudioRecord.STATE_INITIALIZED) {
            Log.w(TAG, "couldn't open the mic");
            recorder.release();
            return;
        }
        recorder.startRecording();
        short[] frame = new short[WakeDetector.FRAME];
        long deafUntil = 0;
        try {
            detector.reset();
            while (listening) {
                int read = 0;
                while (read < frame.length && listening) {
                    int n = recorder.read(frame, read, frame.length - read);
                    if (n < 0) throw new IOException("the mic stopped: " + n);
                    read += n;
                }
                if (!listening) break;
                float score = detector.score(frame);
                if (score >= THRESHOLD && SystemClock.elapsedRealtime() >= deafUntil) {
                    Log.i(TAG, "heard it, " + score);
                    deafUntil = SystemClock.elapsedRealtime() + DEAF_MS;
                    main.post(this::heard);
                }
            }
        } catch (Exception e) {
            Log.e(TAG, "stopped listening", e);
        } finally {
            recorder.stop();
            recorder.release();
        }
    }

    // In front, huvu hears of it at once. Behind another app, it comes to
    // the front, which Android allows a background app only once the person
    // let it show over other apps.
    private void heard() {
        if (WakeWordPlugin.inFront()) {
            WakeWordPlugin.heard();
            return;
        }
        if (!Settings.canDrawOverlays(this)) {
            Log.i(TAG, "heard it behind another app, and may not come to the front");
            return;
        }
        Intent open = new Intent(this, MainActivity.class)
            .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
            .putExtra(WakeWordPlugin.HEARD, true);
        startActivity(open);
    }

    private Notification notification() {
        NotificationManagerCompat.from(this).createNotificationChannel(
            new NotificationChannelCompat.Builder(CHANNEL, NotificationManagerCompat.IMPORTANCE_MIN)
                .setName("Listening for hey huvu")
                .setShowBadge(false)
                .build()
        );
        int immutable = PendingIntent.FLAG_IMMUTABLE;
        PendingIntent open = PendingIntent.getActivity(this, 0, new Intent(this, MainActivity.class), immutable);
        PendingIntent off = PendingIntent.getService(this, 0, new Intent(this, WakeService.class).setAction(TURN_OFF), immutable);
        return new NotificationCompat.Builder(this, CHANNEL)
            .setSmallIcon(R.drawable.ic_stat_huvu)
            .setContentTitle("Listening for hey huvu")
            .setContentText("While the screen is on and unlocked")
            .setContentIntent(open)
            .addAction(0, "Turn off", off)
            .setOngoing(true)
            .setPriority(NotificationCompat.PRIORITY_MIN)
            .setForegroundServiceBehavior(NotificationCompat.FOREGROUND_SERVICE_IMMEDIATE)
            .build();
    }

    private byte[] asset(String path) throws IOException {
        try (InputStream in = getAssets().open(path)) {
            ByteArrayOutputStream out = new ByteArrayOutputStream();
            byte[] chunk = new byte[64 * 1024];
            int n;
            while ((n = in.read(chunk)) > 0) {
                out.write(chunk, 0, n);
            }
            return out.toByteArray();
        }
    }
}
