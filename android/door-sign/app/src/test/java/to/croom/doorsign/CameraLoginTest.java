package to.croom.doorsign;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertTrue;
import static org.junit.Assert.assertNotEquals;
import static org.junit.Assert.assertNull;

import org.json.JSONObject;
import org.junit.Test;

public class CameraLoginTest {

    static final CameraLogin LOGIN = new CameraLogin("camera", "p&ss<w>rd1", "Conference Room B door sign");

    @Test public void theRoomsBlockIsALogin() throws Exception {
        JSONObject block = new JSONObject().put("user", "camera").put("password", "p&ss<w>rd1").put("name", "Conference Room B door sign");
        assertEquals(LOGIN, CameraLogin.from(block));
    }

    @Test public void aBlockWithoutUserOrPasswordIsNone() throws Exception {
        assertNull(CameraLogin.from(new JSONObject().put("user", "camera").put("password", " ")));
        assertNull(CameraLogin.from(new JSONObject().put("password", "secret-pw1")));
        assertNull(CameraLogin.from("camera"));
        assertNull(CameraLogin.from(null));
        assertNull(CameraLogin.from(JSONObject.NULL));
    }

    @Test public void aBlockWithoutANameHasABlankOne() throws Exception {
        assertEquals("", CameraLogin.from(new JSONObject().put("user", "camera").put("password", "secret-pw1")).name);
    }

    @Test public void theLoginSurvivesARestart() {
        MapStore store = new MapStore();
        assertNull(CameraLogin.load(store));
        LOGIN.save(store);
        assertEquals(LOGIN, CameraLogin.load(store));
    }

    @Test public void aChangedPasswordIsAnotherLogin() {
        assertNotEquals(LOGIN, new CameraLogin("camera", "other-pw1", "Conference Room B door sign"));
    }

    @Test public void theRoomSaysWhetherToSendSound() throws Exception {
        JSONObject block = new JSONObject().put("user", "camera").put("password", "secret-pw1").put("name", "Room 1 door sign");
        assertFalse(CameraLogin.from(block).sound);                     // a room from before the switch, or with it off
        assertTrue(CameraLogin.from(block.put("sound", true)).sound);
        assertFalse(CameraLogin.from(block.put("sound", "yes")).sound);  // only a real true turns the microphone on
    }

    @Test public void soundSurvivesARestartAndTurningItOnIsAnotherLogin() {
        CameraLogin withSound = new CameraLogin("camera", "p&ss<w>rd1", "Conference Room B door sign", true);
        MapStore store = new MapStore();
        withSound.save(store);
        assertEquals(withSound, CameraLogin.load(store));
        assertNotEquals(LOGIN, withSound);                               // the camera starts again, with the microphone
        LOGIN.save(store);
        assertFalse(CameraLogin.load(store).sound);
    }

    @Test public void soundCannotWords() {
        CameraLogin withSound = new CameraLogin("camera", "secret-pw1", "", true);
        assertEquals("Sound off: Android has not granted the app the microphone",
                Panel.soundCannot(withSound, "Android has not granted the app the microphone"));
        assertNull(Panel.soundCannot(withSound, null));
        assertNull(Panel.soundCannot(LOGIN, "stale"));                   // the room records no sound: nothing to say
        assertNull(Panel.soundCannot(null, "stale"));
    }

    @Test public void cameraCannotWords() {
        assertEquals("Camera off: this panel is not the device owner", Panel.cameraCannot(false, LOGIN, null));
        assertEquals("Camera off: set the door sign camera password in Settings › Devices", Panel.cameraCannot(true, null, null));
        assertEquals("Camera off: the camera did not start: busy", Panel.cameraCannot(true, LOGIN, "busy"));
        assertNull(Panel.cameraCannot(true, LOGIN, null));
    }
}
