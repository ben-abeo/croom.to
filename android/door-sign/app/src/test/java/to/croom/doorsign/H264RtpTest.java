package to.croom.doorsign;

import static org.junit.Assert.assertArrayEquals;
import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertTrue;

import java.io.ByteArrayOutputStream;
import java.util.List;
import org.junit.Test;

public class H264RtpTest {

    static int u16(byte[] p, int at) {
        return ((p[at] & 0xff) << 8) | (p[at + 1] & 0xff);
    }

    static long u32(byte[] p, int at) {
        return ((long) u16(p, at) << 16) | u16(p, at + 2);
    }

    @Test public void smallNalIsOnePacket() {
        H264Rtp rtp = new H264Rtp(0x11223344);
        byte[] nal = {0x65, 1, 2, 3};
        List<byte[]> packets = rtp.packets(nal, 1_000_000, true);
        assertEquals(1, packets.size());
        byte[] p = packets.get(0);
        assertEquals(0x80, p[0] & 0xff);                    // version 2
        assertEquals(0x80 | 96, p[1] & 0xff);               // marker, payload type 96
        assertEquals(0, u16(p, 2));
        assertEquals(90_000, u32(p, 4));                    // one second at 90 kHz
        assertEquals(0x11223344L, u32(p, 8));
        assertEquals(12 + nal.length, p.length);
        assertEquals(0x65, p[12]);
        assertEquals(96, rtp.packets(nal, 1_000_000, false).get(0)[1] & 0xff);   // no marker mid-frame
    }

    @Test public void largeNalIsFragmented() throws Exception {
        H264Rtp rtp = new H264Rtp(1);
        byte[] nal = new byte[5000];
        nal[0] = 0x65;                                       // nal_ref_idc 3, IDR slice
        for (int i = 1; i < nal.length; i++) nal[i] = (byte) i;
        List<byte[]> packets = rtp.packets(nal, 0, true);
        assertTrue(packets.size() > 1);
        ByteArrayOutputStream whole = new ByteArrayOutputStream();
        whole.write((packets.get(0)[12] & 0xe0) | (packets.get(0)[13] & 0x1f));   // the NAL header, from FU indicator and FU header
        for (int i = 0; i < packets.size(); i++) {
            byte[] p = packets.get(i);
            assertTrue(p.length <= H264Rtp.MTU);
            assertEquals(28, p[12] & 0x1f);                  // FU-A
            assertEquals(0x60, p[12] & 0xe0);                // the NAL's nal_ref_idc kept
            assertEquals(i == 0, (p[13] & 0x80) != 0);       // start
            assertEquals(i == packets.size() - 1, (p[13] & 0x40) != 0);   // end
            assertEquals(i == packets.size() - 1, (p[1] & 0x80) != 0);    // marker on the last only
            assertEquals(i, u16(p, 2));
            whole.write(p, 14, p.length - 14);
        }
        assertArrayEquals(nal, whole.toByteArray());
    }

    @Test public void sequenceWraps() {
        H264Rtp rtp = new H264Rtp(1);
        byte[] nal = {0x41, 1};
        int last = -1;
        for (int i = 0; i < 65_538; i++) last = u16(rtp.packets(nal, 0, true).get(0), 2);
        assertEquals(1, last);                               // 0..65535, then 0, 1
    }

    @Test public void annexBSplitsOnThreeAndFourByteStartCodes() {
        byte[] stream = {0, 0, 0, 1, 0x67, 1, 2, 0, 0, 1, 0x68, 3, 0, 0, 0, 1, 0x65, 4, 5};
        List<byte[]> nals = H264Rtp.nals(stream);
        assertEquals(3, nals.size());
        assertArrayEquals(new byte[] {0x67, 1, 2}, nals.get(0));
        assertArrayEquals(new byte[] {0x68, 3}, nals.get(1));
        assertArrayEquals(new byte[] {0x65, 4, 5}, nals.get(2));
        assertEquals(0, H264Rtp.nals(new byte[] {0, 0, 1}).size());
    }

    @Test public void interleavedFraming() {
        byte[] rtp = new byte[300];
        byte[] framed = H264Rtp.interleaved(rtp, 0);
        assertEquals('$', framed[0]);
        assertEquals(0, framed[1]);
        assertEquals(300, u16(framed, 2));
        assertEquals(304, framed.length);
        assertEquals(2, H264Rtp.interleaved(rtp, 2)[1]);                // the sound's channel
    }
}
