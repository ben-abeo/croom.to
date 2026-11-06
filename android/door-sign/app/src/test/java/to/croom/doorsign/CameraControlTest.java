package to.croom.doorsign;

import static org.junit.Assert.assertEquals;

import org.junit.Test;

public class CameraControlTest {

    @Test public void theServiceStartsOnlyWithTheCameraGranted() {
        assertEquals(CameraControl.Action.START, CameraControl.decide(true, true, true));
        // Android 14 throws from startForeground without the camera, and a service that never reaches it crashes the sign's process
        assertEquals(CameraControl.Action.NOT_GRANTED, CameraControl.decide(true, true, false));
        assertEquals(CameraControl.Action.STOP, CameraControl.decide(false, true, true));
        assertEquals(CameraControl.Action.STOP, CameraControl.decide(true, false, true));
        assertEquals(CameraControl.Action.STOP, CameraControl.decide(false, false, false));
    }
}
