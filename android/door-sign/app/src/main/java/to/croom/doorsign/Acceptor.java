package to.croom.doorsign;

import java.io.IOException;
import java.net.ServerSocket;
import java.net.Socket;
import java.util.concurrent.Semaphore;

/**
 * The camera's servers take connections here: each on a thread of its own, at most `max` at a time. A connection past that is closed at
 * once, so nothing on the LAN can pile up threads in the sign's process (spec 2026-11-06: the camera never takes the sign down).
 */
final class Acceptor {

    interface Handler {
        void handle(Socket socket) throws IOException;
    }

    private Acceptor() {}

    /** Serves until the server socket closes, which ends it with that IOException. */
    static void serve(ServerSocket server, int max, String name, Handler handler) throws IOException {
        Semaphore permits = new Semaphore(max);
        while (true) {
            Socket socket = server.accept();
            if (!permits.tryAcquire()) {
                close(socket);
                continue;
            }
            try {
                Thread thread = new Thread(() -> {
                    try {
                        handler.handle(socket);
                    } catch (IOException | RuntimeException e) {
                        // the client left, or sent something that is not a request
                    } finally {
                        close(socket);
                        permits.release();
                    }
                }, name);
                thread.setDaemon(true);
                thread.start();
            } catch (Throwable e) {          // no thread to be had: turn this one away rather than end the process
                permits.release();
                close(socket);
            }
        }
    }

    private static void close(Socket socket) {
        try {
            socket.close();
        } catch (IOException ignored) {
        }
    }
}
