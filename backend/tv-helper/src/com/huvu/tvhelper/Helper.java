package com.huvu.tvhelper;

import android.app.UiAutomation;
import android.graphics.Bitmap;
import android.graphics.Rect;
import android.os.HandlerThread;
import android.os.Looper;
import android.os.SystemClock;
import android.util.Base64;
import android.view.InputDevice;
import android.view.InputEvent;
import android.view.KeyCharacterMap;
import android.view.KeyEvent;
import android.view.MotionEvent;
import android.view.accessibility.AccessibilityNodeInfo;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.OutputStream;
import java.io.PrintStream;
import java.lang.reflect.Constructor;
import java.lang.reflect.InvocationTargetException;
import java.lang.reflect.Method;
import java.util.Locale;

/**
 * huvu's helper on the TV box, which the backend runs through `app_process`
 * as the shell user, once per action. Each argument is one step, done in
 * order:
 *
 *   key:<code>,<code>      press keys, the way the remote does
 *   tap:<x>,<y>            touch a point, in the screen's own pixels
 *   text:<base64>          type text into the field that has the highlight
 *   select:<base64 json>   highlight an element and press select (see select)
 *   front:<package>,<ms>   wait until that app's window is in front
 *   until:<b64 json>,<ms>  wait until an element (as for select) shows
 *   idle:<ms>              wait until the screen stops changing, at most ms
 *   sleep:<ms>             wait
 *   look:<w>x<h>,<picture> the tree of the window in front, and a
 *                          screenshot of that size: always, never, or auto
 *                          (only when the tree names next to nothing)
 *
 * Each answer is a section on stdout, `HUVU <name> <length>\n` and that many
 * bytes, so a JPEG passes unharmed. A step that fails answers an `error`
 * section naming it, the steps after it are skipped, and a `look` among them
 * still runs, so the backend sees where the box was left.
 *
 * Input goes through UiAutomation, the way `uiautomator` drives a device, so
 * no step starts a second Java VM the way `input` does. On Android 5.1 a
 * UiAutomation pushes every other accessibility service aside, and Fire OS
 * doesn't bring them back when it lets go: a run turns VoiceView off until
 * accessibility is turned off and on again. The screenshot is
 * SurfaceControl's, scaled on the device, so what crosses the Stick's slow
 * ADB link is a small JPEG.
 */
public final class Helper {
    private static OutputStream out;
    private static UiAutomation automation;

    public static void main(String[] args) throws Exception {
        out = System.out;
        // Anything the framework prints must not land inside a section.
        System.setOut(new PrintStream(new ByteArrayOutputStream()));
        HandlerThread thread = new HandlerThread("huvu");
        thread.start();
        try {
            automation = connect(thread.getLooper());
            boolean failed = false;
            for (int i = 0; i < args.length; i++) {
                int colon = args[i].indexOf(':');
                String name = colon < 0 ? args[i] : args[i].substring(0, colon);
                String value = colon < 0 ? "" : args[i].substring(colon + 1);
                if (failed && !name.equals("look")) {
                    continue;
                }
                try {
                    step(name, value);
                } catch (Throwable err) {
                    failed = true;
                    section("error", ("step " + (i + 1) + " (" + name + "): " + reason(err)).getBytes("UTF-8"));
                }
            }
        } catch (Throwable err) {
            section("error", reason(err).getBytes("UTF-8"));
        } finally {
            if (automation != null) {
                try {
                    UiAutomation.class.getMethod("disconnect").invoke(automation);
                } catch (Throwable ignored) {
                    // The process ends next anyway.
                }
            }
            out.flush();
            thread.quit();
        }
        System.exit(0);
    }

    private static String reason(Throwable err) {
        while (err instanceof InvocationTargetException && err.getCause() != null) {
            err = err.getCause();
        }
        return err instanceof IllegalStateException ? err.getMessage() : err.getClass().getSimpleName() + ": " + err.getMessage();
    }

    private static UiAutomation connect(Looper looper) throws Exception {
        Class<?> connectionClass = Class.forName("android.app.UiAutomationConnection");
        Class<?> connectionInterface = Class.forName("android.app.IUiAutomationConnection");
        Constructor<UiAutomation> make = UiAutomation.class.getDeclaredConstructor(Looper.class, connectionInterface);
        make.setAccessible(true);
        UiAutomation made = make.newInstance(looper, connectionClass.newInstance());
        UiAutomation.class.getMethod("connect").invoke(made);
        return made;
    }

    private static void step(String name, String value) throws Exception {
        switch (name) {
            case "key":
                for (String code : value.split(",")) {
                    key(Integer.parseInt(code.trim()));
                }
                break;
            case "tap": {
                String[] xy = value.split(",");
                tap(Float.parseFloat(xy[0]), Float.parseFloat(xy[1]));
                break;
            }
            case "text":
                text(decode(value));
                break;
            case "select":
                section("selected", select(new JSONArray(decode(value))).toString().getBytes("UTF-8"));
                break;
            case "front": {
                String[] parts = value.split(",");
                front(parts[0], Long.parseLong(parts[1]));
                break;
            }
            case "until": {
                String[] parts = value.split(",");
                until(new JSONArray(decode(parts[0])), Long.parseLong(parts[1]));
                break;
            }
            case "idle":
                idle(Long.parseLong(value));
                break;
            case "sleep":
                SystemClock.sleep(Long.parseLong(value));
                break;
            case "look": {
                String[] parts = value.split(",");
                String[] size = parts[0].split("x");
                look(Integer.parseInt(size[0]), Integer.parseInt(size[1]), parts[1]);
                break;
            }
            default:
                throw new IllegalArgumentException("no step " + name);
        }
    }

    private static String decode(String value) throws Exception {
        return new String(Base64.decode(value, Base64.DEFAULT), "UTF-8");
    }

    private static void section(String name, byte[] body) throws Exception {
        out.write(("HUVU " + name + " " + body.length + "\n").getBytes("UTF-8"));
        out.write(body);
        out.flush();
    }

    // Input

    private static void key(int code) {
        long now = SystemClock.uptimeMillis();
        inject(new KeyEvent(now, now, KeyEvent.ACTION_DOWN, code, 0, 0, KeyCharacterMap.VIRTUAL_KEYBOARD, 0, 0, InputDevice.SOURCE_KEYBOARD));
        inject(new KeyEvent(now, SystemClock.uptimeMillis(), KeyEvent.ACTION_UP, code, 0, 0, KeyCharacterMap.VIRTUAL_KEYBOARD, 0, 0, InputDevice.SOURCE_KEYBOARD));
    }

    private static void tap(float x, float y) {
        long now = SystemClock.uptimeMillis();
        MotionEvent down = MotionEvent.obtain(now, now, MotionEvent.ACTION_DOWN, x, y, 0);
        down.setSource(InputDevice.SOURCE_TOUCHSCREEN);
        inject(down);
        MotionEvent up = MotionEvent.obtain(now, SystemClock.uptimeMillis(), MotionEvent.ACTION_UP, x, y, 0);
        up.setSource(InputDevice.SOURCE_TOUCHSCREEN);
        inject(up);
    }

    private static void text(String text) {
        KeyEvent[] events = KeyCharacterMap.load(KeyCharacterMap.VIRTUAL_KEYBOARD).getEvents(text.toCharArray());
        if (events == null) {
            throw new IllegalStateException("these characters can't be typed");
        }
        for (KeyEvent event : events) {
            inject(KeyEvent.changeTimeRepeat(event, SystemClock.uptimeMillis(), 0));
        }
    }

    private static void inject(InputEvent event) {
        if (!automation.injectInputEvent(event, true)) {
            throw new IllegalStateException("the box refused the input");
        }
    }

    // Waiting

    private static void idle(long most) {
        try {
            automation.waitForIdle(300, most);
        } catch (java.util.concurrent.TimeoutException ignored) {
            // Something on screen keeps moving: it is as settled as it gets.
        }
    }

    private static AccessibilityNodeInfo root(long most) {
        long until = SystemClock.uptimeMillis() + most;
        while (true) {
            AccessibilityNodeInfo root = automation.getRootInActiveWindow();
            if (root != null || SystemClock.uptimeMillis() > until) {
                return root;
            }
            SystemClock.sleep(100);
        }
    }

    private static void front(String pkg, long most) {
        long until = SystemClock.uptimeMillis() + most;
        while (SystemClock.uptimeMillis() < until) {
            AccessibilityNodeInfo root = automation.getRootInActiveWindow();
            if (root != null && pkg.equals(String.valueOf(root.getPackageName()))) {
                return;
            }
            SystemClock.sleep(150);
        }
        throw new IllegalStateException(pkg + " didn't come to the front");
    }

    private static void until(JSONArray wants, long most) throws Exception {
        long until = SystemClock.uptimeMillis() + most;
        while (true) {
            AccessibilityNodeInfo root = automation.getRootInActiveWindow();
            if (root != null && first(root, wants) != null) {
                return;
            }
            if (SystemClock.uptimeMillis() > until) {
                throw new IllegalStateException("it didn't show: " + name(wants));
            }
            SystemClock.sleep(200);
        }
    }

    // The screen

    private static void look(int width, int height, String picture) throws Exception {
        JSONObject tree = new JSONObject();
        JSONArray nodes = new JSONArray();
        AccessibilityNodeInfo root = root(1000);
        int named = 0;
        if (root != null) {
            tree.put("package", String.valueOf(root.getPackageName()));
            named = walk(root, -1, nodes);
        }
        tree.put("nodes", nodes);
        section("tree", tree.toString().getBytes("UTF-8"));
        // A screenshot only where the tree doesn't tell the model what is on
        // the screen.
        if (picture.equals("always") || (picture.equals("auto") && named < 3)) {
            byte[] jpeg = shot(width, height);
            if (jpeg == null) {
                section("hidden", new byte[0]);
            } else {
                section("shot", jpeg);
            }
        }
    }

    /** Puts every visible node in `nodes`, depth first, and answers how many have a name. */
    private static int walk(AccessibilityNodeInfo node, int parent, JSONArray nodes) throws Exception {
        if (node == null || nodes.length() > 1500) {
            return 0;
        }
        int index = nodes.length();
        JSONObject one = describe(node, parent);
        nodes.put(one);
        int named = one.has("text") || one.has("desc") ? 1 : 0;
        for (int i = 0; i < node.getChildCount(); i++) {
            AccessibilityNodeInfo child = node.getChild(i);
            if (child != null && child.isVisibleToUser()) {
                named += walk(child, index, nodes);
            }
        }
        return named;
    }

    private static JSONObject describe(AccessibilityNodeInfo node, int parent) throws Exception {
        Rect bounds = new Rect();
        node.getBoundsInScreen(bounds);
        JSONObject one = new JSONObject();
        one.put("p", parent);
        one.put("class", String.valueOf(node.getClassName()));
        if (node.getText() != null && node.getText().length() > 0) {
            one.put("text", node.getText().toString());
        }
        if (node.getContentDescription() != null && node.getContentDescription().length() > 0) {
            one.put("desc", node.getContentDescription().toString());
        }
        if (node.getViewIdResourceName() != null) {
            one.put("id", node.getViewIdResourceName());
        }
        one.put("bounds", new JSONArray().put(bounds.left).put(bounds.top).put(bounds.right).put(bounds.bottom));
        if (node.isFocused()) one.put("focused", true);
        if (node.isFocusable()) one.put("focusable", true);
        if (node.isClickable()) one.put("clickable", true);
        if (node.isSelected()) one.put("selected", true);
        if (node.isCheckable()) one.put("checked", node.isChecked());
        if (node.isEditable()) one.put("editable", true);
        if (!node.isEnabled()) one.put("disabled", true);
        return one;
    }

    private static byte[] shot(int width, int height) throws Exception {
        Bitmap bitmap = null;
        try {
            Method screenshot = Class.forName("android.view.SurfaceControl").getMethod("screenshot", int.class, int.class);
            bitmap = (Bitmap) screenshot.invoke(null, width, height);
        } catch (Throwable ignored) {
            // Not on this Android: UiAutomation's full-size one, scaled here.
        }
        if (bitmap == null) {
            Bitmap full = automation.takeScreenshot();
            if (full == null) {
                // A window that hides itself from screenshots, as Netflix does.
                return null;
            }
            bitmap = Bitmap.createScaledBitmap(full, width, height, true);
        }
        ByteArrayOutputStream jpeg = new ByteArrayOutputStream();
        bitmap.compress(Bitmap.CompressFormat.JPEG, 70, jpeg);
        return jpeg.toByteArray();
    }

    // Selecting an element

    /**
     * Highlights an element of the window in front and presses select. Each
     * of `wants` describes one: `class`, `text`, `desc` and `id` must equal
     * the node's when given, `label` matches its text or description without
     * regard to case, and `bounds` picks the nearest of several. The first of
     * `wants` that matches a node is the one. TV apps act on the remote's
     * select key on the highlighted item, and many take an accessibility
     * click and do nothing with it, so the click is the fallback.
     */
    private static JSONObject select(JSONArray wants) throws Exception {
        AccessibilityNodeInfo root = root(1500);
        if (root == null) {
            throw new IllegalStateException("no window is in front");
        }
        AccessibilityNodeInfo node = first(root, wants);
        if (node == null) {
            throw new IllegalStateException("no such element on the screen: " + name(wants) + " (the screen may have changed since the last look)");
        }
        JSONObject done = new JSONObject();
        done.put("label", node.getText() != null ? node.getText().toString() : String.valueOf(node.getContentDescription()));
        for (AccessibilityNodeInfo at = node; at != null; at = at.getParent()) {
            if (at.isFocusable()) {
                if (!at.isFocused()) {
                    at.performAction(AccessibilityNodeInfo.ACTION_FOCUS);
                    SystemClock.sleep(200);
                    at.refresh();
                }
                if (at.isFocused()) {
                    key(KeyEvent.KEYCODE_DPAD_CENTER);
                    return done.put("by", "select");
                }
                break;
            }
        }
        for (AccessibilityNodeInfo at = node; at != null; at = at.getParent()) {
            if (at.isClickable() && at.performAction(AccessibilityNodeInfo.ACTION_CLICK)) {
                return done.put("by", "click");
            }
        }
        throw new IllegalStateException("that element takes neither the highlight nor a click");
    }

    /** The node the first of `wants` that matches anything matches. */
    private static AccessibilityNodeInfo first(AccessibilityNodeInfo root, JSONArray wants) throws Exception {
        for (int i = 0; i < wants.length(); i++) {
            AccessibilityNodeInfo[] best = new AccessibilityNodeInfo[1];
            long[] distance = {Long.MAX_VALUE};
            find(root, wants.getJSONObject(i), best, distance);
            if (best[0] != null) {
                return best[0];
            }
        }
        return null;
    }

    private static String name(JSONArray wants) throws Exception {
        JSONObject want = wants.getJSONObject(0);
        for (String key : new String[] {"label", "text", "desc", "id"}) {
            if (want.has(key) && !want.isNull(key)) {
                return want.getString(key);
            }
        }
        return "that element";
    }

    private static void find(AccessibilityNodeInfo node, JSONObject want, AccessibilityNodeInfo[] best, long[] bestDistance) throws Exception {
        if (node == null) {
            return;
        }
        if (matches(node, want)) {
            long distance = 0;
            JSONArray wanted = want.optJSONArray("bounds");
            if (wanted != null) {
                Rect bounds = new Rect();
                node.getBoundsInScreen(bounds);
                long dx = bounds.centerX() - (wanted.getInt(0) + wanted.getInt(2)) / 2;
                long dy = bounds.centerY() - (wanted.getInt(1) + wanted.getInt(3)) / 2;
                distance = dx * dx + dy * dy;
            }
            // Without bounds, the first in the tree's order wins.
            if (distance < bestDistance[0]) {
                bestDistance[0] = distance;
                best[0] = node;
            }
        }
        for (int i = 0; i < node.getChildCount(); i++) {
            AccessibilityNodeInfo child = node.getChild(i);
            if (child != null && child.isVisibleToUser()) {
                find(child, want, best, bestDistance);
            }
        }
    }

    private static boolean matches(AccessibilityNodeInfo node, JSONObject want) {
        if (want.has("label")) {
            String label = want.optString("label").toLowerCase(Locale.ROOT);
            boolean text = node.getText() != null && node.getText().toString().toLowerCase(Locale.ROOT).equals(label);
            boolean desc = node.getContentDescription() != null && node.getContentDescription().toString().toLowerCase(Locale.ROOT).equals(label);
            if (!text && !desc) {
                return false;
            }
        }
        return same(want, "class", node.getClassName()) && same(want, "text", node.getText())
                && same(want, "desc", node.getContentDescription()) && same(want, "id", node.getViewIdResourceName());
    }

    /** A key `want` lacks matches anything; one it has matches only the same value. */
    private static boolean same(JSONObject want, String key, CharSequence value) {
        if (!want.has(key)) {
            return true;
        }
        if (want.isNull(key)) {
            return value == null || value.length() == 0;
        }
        return value != null && want.optString(key).equals(value.toString());
    }
}
