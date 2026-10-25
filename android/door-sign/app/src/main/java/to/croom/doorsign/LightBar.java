package to.croom.doorsign;

/**
 * The light bar round the SMT1019 panel, in the colour of the room's state. The colours are the app's alone: the sign page
 * passes the state's name (sign.js, setLight), never levels.
 */
final class LightBar {

    /** The panel's light controller. LedDevice is the real one; a tablet without it answers false to open(). */
    interface Device {
        boolean open();

        void set(int channel, int level);

        void close();
    }

    // The controller's channels and its brightest level, as the panel's own LED test drives them (libjnielc ledseek).
    static final int RED = 161;
    static final int GREEN = 162;
    static final int BLUE = 163;
    static final int FULL = 15;

    private final Device device;

    LightBar(Device device) {
        this.device = device;
    }

    /**
     * Red, green and blue levels for a state the sign shows: free green, soon yellow, occupied red. Anything else (offline,
     * loading, or a state from a newer sign) is off, as the sign itself colours nothing then.
     */
    static int[] levels(String state) {
        if ("free".equals(state)) return new int[] {0, FULL, 0};
        if ("soon".equals(state)) return new int[] {FULL, FULL, 0};
        if ("occupied".equals(state)) return new int[] {FULL, 0, 0};
        return new int[] {0, 0, 0};
    }

    synchronized void show(String state) {
        int[] levels = levels(state);
        if (!device.open()) return;
        try {
            device.set(RED, levels[0]);
            device.set(GREEN, levels[1]);
            device.set(BLUE, levels[2]);
        } finally {
            device.close();
        }
    }

    void off() {
        show(null);
    }
}
