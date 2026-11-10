package to.croom.doorsign;

import static org.junit.Assert.assertArrayEquals;
import static org.junit.Assert.assertEquals;

import java.util.Arrays;
import org.junit.Test;

public class AacRtpTest {

    static int u16(byte[] b, int at) {
        return ((b[at] & 0xff) << 8) | (b[at + 1] & 0xff);
    }

    static long u32(byte[] b, int at) {
        return ((long) u16(b, at) << 16) | u16(b, at + 2);
    }

    @Test public void oneAccessUnitIsOnePacketWithItsAuHeader() {
        byte[] frame = new byte[300];
        Arrays.fill(frame, (byte) 7);
        byte[] p = new AacRtp(0x11223344).packet(frame, 1_000_000);
        assertEquals(12 + 4 + 300, p.length);
        assertEquals(0x80, p[0] & 0xff);
        assertEquals(0x80 | 97, p[1] & 0xff);              // marker: the packet holds a whole frame
        assertEquals(16_000, u32(p, 4));                    // one second on the 16 kHz clock
        assertEquals(0x11223344L, u32(p, 8));
        assertEquals(16, u16(p, 12));                       // AU-headers-length in bits: one header
        assertEquals(300 << 3, u16(p, 14));                 // AU-size (13 bits), AU-Index 0
        assertArrayEquals(frame, Arrays.copyOfRange(p, 16, p.length));
    }

    @Test public void sequenceCountsAndWraps() {
        AacRtp rtp = new AacRtp(1);
        for (int i = 0; i < 65535; i++) rtp.packet(new byte[1], 0);
        assertEquals(65535, u16(rtp.packet(new byte[1], 0), 2));
        assertEquals(0, u16(rtp.packet(new byte[1], 0), 2));
    }
}
