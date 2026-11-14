package fun.winlife.airemote;

import android.view.View;
import android.view.ViewGroup;
import androidx.core.graphics.Insets;
import androidx.core.view.ViewCompat;
import androidx.core.view.WindowInsetsCompat;
import org.junit.Test;
import org.junit.runner.RunWith;
import org.robolectric.Robolectric;
import org.robolectric.RobolectricTestRunner;
import org.robolectric.android.controller.ActivityController;
import org.robolectric.annotation.Config;
import static org.junit.Assert.*;

@RunWith(RobolectricTestRunner.class)
@Config(sdk = {30, 35})
public class WindowInsetsTest {
    @Test public void nativeContainerHandlesSafeAreasOnceAndPreservesKeyboardUpdates() {
        try (ActivityController<MainActivity> activity = Robolectric.buildActivity(MainActivity.class).setup().visible()) {
            ViewGroup content = activity.get().findViewById(android.R.id.content);
            View root = content.getChildAt(0);
            int safe = WindowInsetsCompat.Type.systemBars() | WindowInsetsCompat.Type.displayCutout();
            WindowInsetsCompat incoming = new WindowInsetsCompat.Builder()
                .setInsets(safe, Insets.of(7, 36, 9, 24))
                .setInsets(WindowInsetsCompat.Type.ime(), Insets.of(0, 0, 0, 280)).build();
            WindowInsetsCompat forwarded = ViewCompat.dispatchApplyWindowInsets(root, incoming);
            assertEquals(36, root.getPaddingTop());
            assertEquals(24, root.getPaddingBottom());
            assertEquals(Insets.NONE, forwarded.getInsets(safe));
            assertEquals(280, forwarded.getInsets(WindowInsetsCompat.Type.ime()).bottom);
            WindowInsetsCompat hidden = new WindowInsetsCompat.Builder(incoming)
                .setInsets(WindowInsetsCompat.Type.ime(), Insets.NONE).build();
            assertEquals(0, ViewCompat.dispatchApplyWindowInsets(root, hidden).getInsets(WindowInsetsCompat.Type.ime()).bottom);
            assertEquals(36, root.getPaddingTop());
        }
    }
    @Test @Config(sdk = 28) public void olderAndroidAlsoAvoidsDuplicatingTheStatusBar() {
        try (ActivityController<MainActivity> activity = Robolectric.buildActivity(MainActivity.class).setup().visible()) {
            ViewGroup content = activity.get().findViewById(android.R.id.content);
            View root = content.getChildAt(0);
            int safe = WindowInsetsCompat.Type.systemBars() | WindowInsetsCompat.Type.displayCutout();
            WindowInsetsCompat incoming = new WindowInsetsCompat.Builder().setInsets(safe, Insets.of(0, 24, 0, 20)).build();
            WindowInsetsCompat forwarded = ViewCompat.dispatchApplyWindowInsets(root, incoming);
            assertEquals(24, root.getPaddingTop());
            assertEquals(0, forwarded.getInsets(safe).top);
        }
    }
}
