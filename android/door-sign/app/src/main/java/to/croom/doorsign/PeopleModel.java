package to.croom.doorsign;

import android.content.Context;
import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.nio.ByteBuffer;
import java.nio.ByteOrder;
import java.util.Arrays;
import java.util.HashMap;
import java.util.Map;
import org.tensorflow.lite.DataType;
import org.tensorflow.lite.Interpreter;

/**
 * The model that finds people in a picture (spec 2026-11-22): TensorFlow's EfficientDet-Lite0, trained on COCO, run on the panel's
 * processor by TensorFlow Lite. It takes a Frame.SIDE square of red, green and blue bytes and gives up to 25 things it found, each
 * with a class and how sure it is; class 0 is a person. The file is the app's asset people.tflite (android/door-sign/README.md says
 * where it is from). Used from one thread only, PeopleWatch's.
 */
final class PeopleModel {

    static final String ASSET = "people.tflite";
    static final float SURE = 0.5f;                 // a person found at least this surely is someone
    static final int PERSON = 0;
    private static final int THREADS = 2;           // of the panel's eight cores: the sign's page keeps the rest

    private final ByteBuffer file;                  // TensorFlow Lite reads the model from it for as long as it runs
    private final Interpreter interpreter;
    private final ByteBuffer input;
    private final float[][] classes;
    private final float[][] scores;
    private final float[] count = new float[1];
    private final Map<Integer, Object> outputs = new HashMap<>();

    PeopleModel(Context context) throws IOException {
        ByteArrayOutputStream bytes = new ByteArrayOutputStream();
        try (InputStream in = context.getAssets().open(ASSET)) {
            byte[] buffer = new byte[64 * 1024];
            for (int n; (n = in.read(buffer)) > 0; ) bytes.write(buffer, 0, n);
        }
        file = ByteBuffer.allocateDirect(bytes.size()).order(ByteOrder.nativeOrder());
        file.put(bytes.toByteArray());
        file.rewind();
        interpreter = new Interpreter(file, new Interpreter.Options().setNumThreads(THREADS));
        try {
            int found = shapes();
            input = ByteBuffer.allocateDirect(Frame.SIDE * Frame.SIDE * 3).order(ByteOrder.nativeOrder());
            classes = new float[1][found];
            scores = new float[1][found];
            outputs.put(0, new float[1][found][4]);         // where each thing is: not used, but the model gives it
            outputs.put(1, classes);
            outputs.put(2, scores);
            outputs.put(3, count);
        } catch (RuntimeException e) {
            interpreter.close();
            throw e;
        }
    }

    /** How many things the model gives, after checking it is the model the app was written for. */
    private int shapes() {
        int[] in = interpreter.getInputTensor(0).shape();
        boolean right = Arrays.equals(in, new int[] {1, Frame.SIDE, Frame.SIDE, 3}) && interpreter.getInputTensor(0).dataType() == DataType.UINT8
                && interpreter.getOutputTensorCount() == 4;
        int found = right ? interpreter.getOutputTensor(1).shape()[1] : 0;
        right = right && Arrays.equals(interpreter.getOutputTensor(0).shape(), new int[] {1, found, 4})
                && Arrays.equals(interpreter.getOutputTensor(1).shape(), new int[] {1, found})
                && Arrays.equals(interpreter.getOutputTensor(2).shape(), new int[] {1, found})
                && Arrays.equals(interpreter.getOutputTensor(3).shape(), new int[] {1});
        if (!right) throw new IllegalStateException(ASSET + " is not the model the app was written for");
        return found;
    }

    /** How sure the model is that a person is in the square (Frame.square), 0 to 1. */
    float look(byte[] rgb) {
        input.rewind();
        input.put(rgb);
        input.rewind();
        interpreter.runForMultipleInputsOutputs(new Object[] {input}, outputs);
        return best(classes[0], scores[0], Math.round(count[0]));
    }

    /** The surest person among the first `count` things found, 0 when there is none. */
    static float best(float[] classes, float[] scores, int count) {
        float best = 0;
        int n = Math.max(0, Math.min(count, Math.min(classes.length, scores.length)));
        for (int i = 0; i < n; i++) {
            if (Math.round(classes[i]) == PERSON && scores[i] > best) best = scores[i];
        }
        return best;
    }

    void close() {
        interpreter.close();
    }
}
