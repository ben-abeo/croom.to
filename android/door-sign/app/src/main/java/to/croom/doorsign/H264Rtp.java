package to.croom.doorsign;

import java.util.ArrayList;
import java.util.Arrays;
import java.util.List;

/**
 * H.264 over RTP (RFC 6184, packetization mode 1) for the camera's stream: a NAL unit that fits goes in one packet, a larger one is cut
 * into FU-A fragments; the marker bit closes each picture. One per RTSP connection, so each has its own sequence numbers.
 */
final class H264Rtp {

    static final int MTU = 1400;
    static final int PAYLOAD_TYPE = 96;
    private static final int HEADER = 12;

    private final int ssrc;
    private int seq;

    H264Rtp(int ssrc) {
        this.ssrc = ssrc;
    }

    /** The RTP packets of one NAL unit (without its start code), stamped at `presentationUs` on the 90 kHz clock. */
    List<byte[]> packets(byte[] nal, long presentationUs, boolean lastOfFrame) {
        long timestamp = presentationUs * 90 / 1000;
        List<byte[]> packets = new ArrayList<>();
        if (nal.length <= MTU - HEADER) {
            byte[] p = header(nal.length, timestamp, lastOfFrame);
            System.arraycopy(nal, 0, p, HEADER, nal.length);
            packets.add(p);
            return packets;
        }
        int indicator = (nal[0] & 0xe0) | 28;
        int type = nal[0] & 0x1f;
        int chunk = MTU - HEADER - 2;
        for (int at = 1; at < nal.length; at += chunk) {
            int size = Math.min(chunk, nal.length - at);
            boolean first = at == 1;
            boolean last = at + size >= nal.length;
            byte[] p = header(2 + size, timestamp, last && lastOfFrame);
            p[HEADER] = (byte) indicator;
            p[HEADER + 1] = (byte) ((first ? 0x80 : 0) | (last ? 0x40 : 0) | type);
            System.arraycopy(nal, at, p, HEADER + 2, size);
            packets.add(p);
        }
        return packets;
    }

    private byte[] header(int payload, long timestamp, boolean marker) {
        byte[] p = new byte[HEADER + payload];
        p[0] = (byte) 0x80;
        p[1] = (byte) ((marker ? 0x80 : 0) | PAYLOAD_TYPE);
        p[2] = (byte) (seq >> 8);
        p[3] = (byte) seq;
        seq = (seq + 1) & 0xffff;
        for (int i = 0; i < 4; i++) {
            p[4 + i] = (byte) (timestamp >> (24 - 8 * i));
            p[8 + i] = (byte) (ssrc >> (24 - 8 * i));
        }
        return p;
    }

    /** The NAL units of an Annex B buffer (as MediaCodec gives them), without their start codes or trailing zeros. */
    static List<byte[]> nals(byte[] annexB) {
        List<byte[]> nals = new ArrayList<>();
        int start = -1;
        int i = 0;
        while (i + 2 < annexB.length) {
            if (annexB[i] == 0 && annexB[i + 1] == 0 && annexB[i + 2] == 1) {
                if (start >= 0) add(nals, annexB, start, i);
                i += 3;
                start = i;
            } else {
                i++;
            }
        }
        if (start >= 0) add(nals, annexB, start, annexB.length);
        return nals;
    }

    private static void add(List<byte[]> nals, byte[] buffer, int from, int to) {
        while (to > from && buffer[to - 1] == 0) to--;          // the leading zero of a 4-byte start code, or trailing_zero_8bits
        if (to > from) nals.add(Arrays.copyOfRange(buffer, from, to));
    }

    /** An RTP packet framed for an RTSP connection: '$', channel 0, its length. */
    static byte[] interleaved(byte[] rtp) {
        byte[] framed = new byte[4 + rtp.length];
        framed[0] = '$';
        framed[2] = (byte) (rtp.length >> 8);
        framed[3] = (byte) rtp.length;
        System.arraycopy(rtp, 0, framed, 4, rtp.length);
        return framed;
    }
}
