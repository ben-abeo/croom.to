package to.croom.doorsign;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertTrue;

import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.OutputStream;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import org.junit.Test;

public class StreamClientsTest {

    /** A connection that takes nothing until it is closed: Protect gone quiet, or a link too slow for the stream. */
    static final class Stuck extends OutputStream {
        final CountDownLatch closed = new CountDownLatch(1);

        @Override public void write(int b) throws IOException {
            try {
                closed.await();
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
            }
            throw new IOException("closed");
        }

        @Override public void close() {
            closed.countDown();
        }
    }

    static void waitFor(java.util.function.BooleanSupplier done) throws InterruptedException {
        long until = System.nanoTime() + TimeUnit.SECONDS.toNanos(5);
        while (!done.getAsBoolean() && System.nanoTime() < until) Thread.sleep(5);
        assertTrue(done.getAsBoolean());
    }

    @Test public void aSlowClientIsDroppedNotWaitedFor() throws Exception {
        StreamClients clients = new StreamClients(10);
        Stuck stuck = new Stuck();
        clients.add(stuck);
        for (int i = 0; i < 20; i++) {
            long start = System.nanoTime();
            clients.send(new byte[] {1}, true);
            assertTrue("send() blocked", System.nanoTime() - start < TimeUnit.MILLISECONDS.toNanos(50));
        }
        waitFor(() -> clients.size() == 0);
        assertEquals(0, stuck.closed.getCount());           // its connection is closed, which frees its writer
    }

    @Test public void aFailingClientIsRemoved() throws Exception {
        StreamClients clients = new StreamClients(10);
        clients.add(new OutputStream() {
            @Override public void write(int b) throws IOException {
                throw new IOException("reset");
            }
        });
        clients.send(new byte[] {1}, true);
        waitFor(() -> clients.size() == 0);
    }

    @Test public void aClientStartsAtAKeyframe() throws Exception {
        StreamClients clients = new StreamClients(10);
        ByteArrayOutputStream out = new ByteArrayOutputStream();
        clients.add(out);
        clients.send(new byte[] {1}, false);                 // the middle of a picture it can't decode: not sent
        clients.send(new byte[] {2}, true);
        clients.send(new byte[] {3}, false);
        waitFor(() -> out.size() == 2);
        assertEquals(2, out.toByteArray()[0]);
        assertEquals(3, out.toByteArray()[1]);
        clients.closeAll();
        assertEquals(0, clients.size());
    }

    @Test public void aClientAddedAfterCloseAllIsClosedAtOnce() throws Exception {
        // A viewer's PLAY that lands just as the camera stops (a new password, a retry) must not hang on an engine that is gone
        StreamClients clients = new StreamClients(10);
        clients.closeAll();
        Stuck late = new Stuck();
        clients.add(late);
        assertEquals(0, clients.size());
        assertEquals(0, late.closed.getCount());
    }
}
