package to.croom.doorsign;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertTrue;

import java.io.InputStream;
import java.net.InetAddress;
import java.net.ServerSocket;
import java.net.Socket;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;
import org.junit.Test;

public class AcceptorTest {

    @Test public void connectionsPastTheLimitAreClosedAtOnce() throws Exception {
        ServerSocket server = new ServerSocket(0, 50, InetAddress.getLoopbackAddress());
        CountDownLatch hold = new CountDownLatch(1);
        AtomicInteger handled = new AtomicInteger();
        CountDownLatch ended = new CountDownLatch(1);
        Thread serving = new Thread(() -> {
            try {
                Acceptor.serve(server, 2, "test", socket -> {
                    handled.incrementAndGet();
                    try {
                        hold.await();
                    } catch (InterruptedException e) {
                        Thread.currentThread().interrupt();
                    }
                });
            } catch (java.io.IOException e) {
                ended.countDown();          // the server closed: serve returns to its caller by IOException
            }
        });
        serving.start();
        Socket a = new Socket(server.getInetAddress(), server.getLocalPort());
        Socket b = new Socket(server.getInetAddress(), server.getLocalPort());
        long until = System.nanoTime() + TimeUnit.SECONDS.toNanos(5);
        while (handled.get() < 2 && System.nanoTime() < until) Thread.sleep(5);
        assertEquals(2, handled.get());

        Socket third = new Socket(server.getInetAddress(), server.getLocalPort());
        third.setSoTimeout(5_000);
        InputStream in = third.getInputStream();
        assertEquals(-1, in.read());       // closed by the panel, not left waiting on a thread of its own
        assertEquals(2, handled.get());

        hold.countDown();                   // the first two end; a new connection is served again
        until = System.nanoTime() + TimeUnit.SECONDS.toNanos(5);
        Socket fourth = null;
        while (handled.get() < 3 && System.nanoTime() < until) {
            if (fourth == null || fourth.isClosed()) fourth = new Socket(server.getInetAddress(), server.getLocalPort());
            Thread.sleep(20);
        }
        assertEquals(3, handled.get());

        server.close();
        assertTrue(ended.await(5, TimeUnit.SECONDS));
        a.close();
        b.close();
        third.close();
        fourth.close();
    }
}
