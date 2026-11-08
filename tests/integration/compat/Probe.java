// SPDX-License-Identifier: GPL-3.0-or-later
package io.github.waydroidmanager.compatprobe;

import android.app.Activity;
import android.content.res.AssetFileDescriptor;
import android.media.MediaCodec;
import android.media.MediaCodecInfo;
import android.media.MediaExtractor;
import android.media.MediaFormat;
import android.os.Bundle;
import android.util.Log;
import android.webkit.WebSettings;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;

public class Probe extends Activity {
    @Override public void onCreate(Bundle state) {
        super.onCreate(state);
        if (getIntent().getBooleanExtra("video", false)) {
            new Thread(this::video).start();
            return;
        }
        CountDownLatch done = new CountDownLatch(1);
        new Thread(() -> {
            try {
                String ua = WebSettings.getDefaultUserAgent(this);
                if (ua != null && ua.contains("Mozilla/5.0")) done.countDown();
            } catch (Throwable e) {
                Log.e("CompatProbe", "UA exception", e);
            }
        }).start();
        // Reproduce Unity's UI callback waiting for its engine thread during ad initialization.
        try {
            Log.i("CompatProbe", done.await(10, TimeUnit.SECONDS)
                    ? "UA PASS" : "UA FAIL: waits for UI thread");
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
        }
    }

    private void video() {
        MediaExtractor extractor = new MediaExtractor();
        MediaCodec codec = null;
        try (AssetFileDescriptor fd = getAssets().openFd("sample.mp4")) {
            extractor.setDataSource(fd.getFileDescriptor(), fd.getStartOffset(), fd.getLength());
            MediaFormat format = null;
            for (int track = 0; track < extractor.getTrackCount(); track++) {
                MediaFormat candidate = extractor.getTrackFormat(track);
                if ("video/avc".equals(candidate.getString(MediaFormat.KEY_MIME))) {
                    format = candidate;
                    extractor.selectTrack(track);
                    break;
                }
            }
            if (format == null) throw new IllegalArgumentException("sample needs an H.264 track");
            format.setInteger(MediaFormat.KEY_COLOR_FORMAT,
                    MediaCodecInfo.CodecCapabilities.COLOR_FormatYUV420Planar);
            codec = MediaCodec.createDecoderByType("video/avc");
            Log.i("CompatProbe", "decoder=" + codec.getName());
            codec.configure(format, null, null, 0);
            codec.start();
            boolean inputDone = false, outputDone = false;
            int frames = 0, inputFrames = 0;
            long bytes = 0;
            long deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(20);
            MediaCodec.BufferInfo info = new MediaCodec.BufferInfo();
            while (!outputDone && System.nanoTime() < deadline) {
                if (!inputDone) {
                    int input = codec.dequeueInputBuffer(10000);
                    if (input >= 0) {
                        int size = extractor.readSampleData(codec.getInputBuffer(input), 0);
                        if (size < 0) {
                            codec.queueInputBuffer(input, 0, 0, 0, MediaCodec.BUFFER_FLAG_END_OF_STREAM);
                            inputDone = true;
                        } else {
                            codec.queueInputBuffer(input, 0, size, extractor.getSampleTime(), 0);
                            inputFrames++;
                            extractor.advance();
                        }
                    }
                }
                int output = codec.dequeueOutputBuffer(info, 10000);
                if (output >= 0) {
                    if (info.size > 0) {
                        frames++;
                        bytes += codec.getOutputBuffer(output).remaining();
                    }
                    outputDone = (info.flags & MediaCodec.BUFFER_FLAG_END_OF_STREAM) != 0;
                    codec.releaseOutputBuffer(output, false);
                }
            }
            Log.i("CompatProbe", "VIDEO "
                    + (outputDone && frames == inputFrames && frames > 0 && bytes > 0 ? "PASS" : "FAIL")
                    + " frames=" + frames + " bytes=" + bytes);
        } catch (Throwable e) {
            Log.e("CompatProbe", "VIDEO FAIL", e);
        } finally {
            if (codec != null) codec.release();
            extractor.release();
        }
    }
}
