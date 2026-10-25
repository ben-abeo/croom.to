package to.croom.doorsign;

import static org.junit.Assert.assertArrayEquals;
import static org.junit.Assert.assertEquals;

import java.util.ArrayList;
import java.util.List;
import org.junit.Test;

public class LightBarTest {

    /** A light controller that records what it is told, as the panel's would carry it out. */
    static final class FakeDevice implements LightBar.Device {
        final List<String> calls = new ArrayList<>();
        boolean present = true;

        @Override public boolean open() {
            calls.add("open");
            return present;
        }

        @Override public void set(int channel, int level) {
            calls.add(channel + "=" + level);
        }

        @Override public void close() {
            calls.add("close");
        }
    }

    @Test public void eachStateTheSignShowsHasItsColour() {
        assertArrayEquals(new int[] {0, 15, 0}, LightBar.levels("free"));
        assertArrayEquals(new int[] {15, 15, 0}, LightBar.levels("soon"));
        assertArrayEquals(new int[] {15, 0, 0}, LightBar.levels("occupied"));
    }

    @Test public void aSignWithNoStatusToShowLeavesTheLightsOff() {
        assertArrayEquals(new int[] {0, 0, 0}, LightBar.levels("offline"));
        assertArrayEquals(new int[] {0, 0, 0}, LightBar.levels("loading"));
        assertArrayEquals(new int[] {0, 0, 0}, LightBar.levels(null));
        assertArrayEquals(new int[] {0, 0, 0}, LightBar.levels("a state from a newer sign"));
    }

    @Test public void aColourIsSetRedGreenBlueBetweenOpenAndClose() {
        FakeDevice device = new FakeDevice();
        new LightBar(device).show("soon");
        assertEquals(List.of("open", "161=15", "162=15", "163=0", "close"), device.calls);
    }

    @Test public void offSetsEveryChannelToNothing() {
        FakeDevice device = new FakeDevice();
        new LightBar(device).off();
        assertEquals(List.of("open", "161=0", "162=0", "163=0", "close"), device.calls);
    }

    @Test public void aTabletWithoutTheLightBarIsLeftAlone() {
        FakeDevice device = new FakeDevice();
        device.present = false;
        new LightBar(device).show("free");
        assertEquals(List.of("open"), device.calls);
    }
}
