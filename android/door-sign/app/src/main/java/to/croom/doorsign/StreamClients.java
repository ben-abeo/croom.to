package to.croom.doorsign;

import java.io.IOException;
import java.io.OutputStream;
import java.util.List;
import java.util.concurrent.ArrayBlockingQueue;
import java.util.concurrent.BlockingQueue;
import java.util.concurrent.CopyOnWriteArrayList;

/**
 * The connections watching the camera (Protect, usually one). The encoder hands each frame to every one of them and never waits:
 * each has a queue of its own and a thread that writes it out, and a connection whose queue is full (Protect gone quiet, a link too
 * slow for the stream) or whose write fails is closed and dropped. Protect connects again by itself. A new connection is sent
 * nothing until a keyframe starts, since it could not decode what comes before.
 */
final class StreamClients {

    final class Client {
        private final OutputStream out;
        private final BlockingQueue<byte[]> queue;
        private final boolean sound;      // it set up the sound track as well as the picture
        private volatile boolean started;
        private volatile boolean closed;

        Client(OutputStream out, boolean sound) {
            this.out = out;
            this.sound = sound;
            this.queue = new ArrayBlockingQueue<>(maxQueued);
            Thread writer = new Thread(this::write, "door-sign-stream-client");
            writer.setDaemon(true);
            writer.start();
        }

        private void write() {
            try {
                while (!closed) out.write(queue.take());
            } catch (IOException | InterruptedException e) {
                // the connection is gone, or was closed below
            }
            close();
        }

        void close() {
            if (closed) return;
            closed = true;
            clients.remove(this);
            queue.clear();
            queue.offer(new byte[0]);       // wakes the writer if it waits on an empty queue
            try {
                out.close();
            } catch (IOException e) {
                // closed already
            }
        }
    }

    private final int maxQueued;
    private final List<Client> clients = new CopyOnWriteArrayList<>();
    private volatile boolean closed;

    /** @param maxQueued how many frames a connection may fall behind before it is dropped */
    StreamClients(int maxQueued) {
        this.maxQueued = maxQueued;
    }

    /** A new viewer of the picture only. */
    Client add(OutputStream out) {
        return add(out, false);
    }

    /** A new viewer, with the sound if it set that track up; one that comes after closeAll() (the camera stopped under it) is closed at once. */
    Client add(OutputStream out, boolean sound) {
        Client client = new Client(out, sound);
        clients.add(client);
        if (closed) client.close();
        return client;
    }

    /** One framed RTP packet to every connection; `startsKeyframe` on the first packet of a keyframe's access unit. */
    void send(byte[] frame, boolean startsKeyframe) {
        for (Client client : clients) {
            if (!client.started && !startsKeyframe) continue;
            client.started = true;
            if (!client.queue.offer(frame)) client.close();
        }
    }

    /** One framed RTP packet of sound, to the connections that set its track up and whose picture has started. */
    void sendSound(byte[] frame) {
        for (Client client : clients) {
            if (client.sound && client.started && !client.queue.offer(frame)) client.close();
        }
    }

    void closeAll() {
        closed = true;
        for (Client client : clients) client.close();
    }

    int size() {
        return clients.size();
    }
}
