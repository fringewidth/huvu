package com.huvu.app.wake;

import ai.onnxruntime.OnnxTensor;
import ai.onnxruntime.OrtEnvironment;
import ai.onnxruntime.OrtException;
import ai.onnxruntime.OrtSession;
import ai.onnxruntime.TensorInfo;
import java.nio.FloatBuffer;
import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.Collections;
import java.util.Iterator;
import java.util.List;
import java.util.Random;

// openWakeWord's streaming detector (AudioFeatures in openwakeword/utils.py
// and Model.predict in model.py), ported step for step. Each 80 ms frame of
// 16 kHz audio becomes 8 mel frames. The last 76 mel frames become one
// embedding of 96 values (Google's speech_embedding), and the wake model
// scores the last 16 embeddings. The two feature models are the same for
// every wake word; only the wake model is trained for "hey huvu".
//
// No Android classes, so it runs on a desktop JVM too.
final class WakeDetector implements AutoCloseable {
    // Samples in one frame: 80 ms at 16 kHz.
    static final int FRAME = 1280;
    // Samples from the frame before that the mel model needs to make 8
    // frames of this one.
    private static final int MEL_CONTEXT = 160 * 3;
    private static final int MEL_BINS = 32;
    private static final int MEL_WINDOW = 76;
    private static final int MEL_STEP = 8;
    private static final int EMBEDDING = 96;
    // openWakeWord scores the first 5 frames after a start 0, since the
    // buffers still hold the noise they start with.
    private static final int WARMUP = 5;

    private final OrtEnvironment env;
    private final OrtSession mel;
    private final OrtSession embedding;
    private final OrtSession wake;
    private final String melInput;
    private final String embeddingInput;
    private final String wakeInput;
    // How many embeddings the wake model reads at once.
    private final int wakeFrames;
    private final Random noise = new Random();

    private final short[] tail = new short[MEL_CONTEXT];
    private int tailLength;
    private final ArrayDeque<float[]> mels = new ArrayDeque<>();
    private final ArrayDeque<float[]> features = new ArrayDeque<>();
    private int scored;

    WakeDetector(byte[] melModel, byte[] embeddingModel, byte[] wakeModel) throws OrtException {
        env = OrtEnvironment.getEnvironment();
        mel = env.createSession(melModel, new OrtSession.SessionOptions());
        embedding = env.createSession(embeddingModel, new OrtSession.SessionOptions());
        wake = env.createSession(wakeModel, new OrtSession.SessionOptions());
        melInput = mel.getInputNames().iterator().next();
        embeddingInput = embedding.getInputNames().iterator().next();
        wakeInput = wake.getInputNames().iterator().next();
        long[] shape = ((TensorInfo) wake.getInputInfo().get(wakeInput).getInfo()).getShape();
        wakeFrames = (int) shape[1];
        reset();
    }

    // Starts over, as openWakeWord does on a reset: no audio before, mel
    // frames of ones, and embeddings of 4 s of quiet noise.
    void reset() throws OrtException {
        tailLength = 0;
        mels.clear();
        for (int i = 0; i < MEL_WINDOW; i++) {
            float[] ones = new float[MEL_BINS];
            Arrays.fill(ones, 1f);
            mels.add(ones);
        }
        float[] quiet = new float[16000 * 4];
        for (int i = 0; i < quiet.length; i++) {
            quiet[i] = noise.nextInt(2000) - 1000;
        }
        List<float[]> spec = melspectrogram(quiet);
        List<float[][]> windows = new ArrayList<>();
        for (int i = 0; i + MEL_WINDOW <= spec.size(); i += MEL_STEP) {
            windows.add(spec.subList(i, i + MEL_WINDOW).toArray(new float[0][]));
        }
        features.clear();
        features.addAll(embed(windows));
        trim(features, wakeFrames);
        scored = 0;
    }

    // How sure the model is that the last frames held the wake word, from
    // 0 to 1. `frame` is FRAME samples.
    float score(short[] frame) throws OrtException {
        float[] audio = new float[tailLength + FRAME];
        for (int i = 0; i < tailLength; i++) {
            audio[i] = tail[i];
        }
        for (int i = 0; i < FRAME; i++) {
            audio[tailLength + i] = frame[i];
        }
        System.arraycopy(frame, FRAME - MEL_CONTEXT, tail, 0, MEL_CONTEXT);
        tailLength = MEL_CONTEXT;

        mels.addAll(melspectrogram(audio));
        trim(mels, MEL_WINDOW);
        features.addAll(embed(Collections.singletonList(mels.toArray(new float[0][]))));
        trim(features, wakeFrames);

        float score = predict();
        if (scored < WARMUP) {
            scored++;
            return 0f;
        }
        return score;
    }

    private List<float[]> melspectrogram(float[] audio) throws OrtException {
        try (OnnxTensor input = OnnxTensor.createTensor(env, FloatBuffer.wrap(audio), new long[] { 1, audio.length });
             OrtSession.Result result = mel.run(Collections.singletonMap(melInput, input))) {
            OnnxTensor out = (OnnxTensor) result.get(0);
            long[] shape = out.getInfo().getShape();
            int frames = (int) shape[shape.length - 2];
            FloatBuffer values = out.getFloatBuffer();
            List<float[]> rows = new ArrayList<>(frames);
            for (int f = 0; f < frames; f++) {
                float[] row = new float[MEL_BINS];
                for (int b = 0; b < MEL_BINS; b++) {
                    // openWakeWord's transform, which brings the ONNX model
                    // close to Google's own.
                    row[b] = values.get() / 10f + 2f;
                }
                rows.add(row);
            }
            return rows;
        }
    }

    private List<float[]> embed(List<float[][]> windows) throws OrtException {
        int batch = windows.size();
        FloatBuffer in = FloatBuffer.allocate(batch * MEL_WINDOW * MEL_BINS);
        for (float[][] window : windows) {
            for (float[] row : window) {
                in.put(row);
            }
        }
        in.rewind();
        try (OnnxTensor input = OnnxTensor.createTensor(env, in, new long[] { batch, MEL_WINDOW, MEL_BINS, 1 });
             OrtSession.Result result = embedding.run(Collections.singletonMap(embeddingInput, input))) {
            FloatBuffer values = ((OnnxTensor) result.get(0)).getFloatBuffer();
            List<float[]> out = new ArrayList<>(batch);
            for (int i = 0; i < batch; i++) {
                float[] e = new float[EMBEDDING];
                values.get(e);
                out.add(e);
            }
            return out;
        }
    }

    private float predict() throws OrtException {
        FloatBuffer in = FloatBuffer.allocate(wakeFrames * EMBEDDING);
        for (float[] f : features) {
            in.put(f);
        }
        in.rewind();
        try (OnnxTensor input = OnnxTensor.createTensor(env, in, new long[] { 1, wakeFrames, EMBEDDING });
             OrtSession.Result result = wake.run(Collections.singletonMap(wakeInput, input))) {
            return ((OnnxTensor) result.get(0)).getFloatBuffer().get(0);
        }
    }

    private static void trim(ArrayDeque<float[]> rows, int keep) {
        Iterator<float[]> it = rows.iterator();
        for (int extra = rows.size() - keep; extra > 0; extra--) {
            it.next();
            it.remove();
        }
    }

    @Override
    public void close() throws OrtException {
        mel.close();
        embedding.close();
        wake.close();
    }
}
