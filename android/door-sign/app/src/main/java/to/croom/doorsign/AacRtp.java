package to.croom.doorsign;

/**
 * The camera's sound over RTP (spec 2026-11-10): AAC-LC as RFC 3640's mpeg4-generic in AAC-hbr mode, the mode cameras use and Protect
 * reads. Each AAC frame (1024 samples, 64 ms at 16 kHz, a few hundred bytes) goes in one packet behind one 16-bit AU header: its size
 * in 13 bits and AU-Index 0. One per RTSP connection, so each has its own sequence numbers, as H264Rtp.
 */
final class AacRtp {

    static final int PAYLOAD_TYPE = 97;
    private static final int HEADER = 12;

    private final int ssrc;
    private int seq;

    AacRtp(int ssrc) {
        this.ssrc = ssrc;
    }

    /** The packet of one raw AAC frame (no ADTS header), stamped at `presentationUs` on the sample-rate clock. */
    byte[] packet(byte[] frame, long presentationUs) {
        long timestamp = presentationUs * Onvif.SAMPLE_RATE / 1_000_000;
        byte[] p = new byte[HEADER + 4 + frame.length];
        p[0] = (byte) 0x80;
        p[1] = (byte) (0x80 | PAYLOAD_TYPE);
        p[2] = (byte) (seq >> 8);
        p[3] = (byte) seq;
        seq = (seq + 1) & 0xffff;
        for (int i = 0; i < 4; i++) {
            p[4 + i] = (byte) (timestamp >> (24 - 8 * i));
            p[8 + i] = (byte) (ssrc >> (24 - 8 * i));
        }
        p[HEADER + 1] = 16;                                   // AU-headers-length: one 16-bit header
        p[HEADER + 2] = (byte) (frame.length >> 5);           // AU-size in the top 13 bits, AU-Index 0 in the low 3
        p[HEADER + 3] = (byte) (frame.length << 3);
        System.arraycopy(frame, 0, p, HEADER + 4, frame.length);
        return p;
    }
}
