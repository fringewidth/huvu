package com.huvu.tv;

import android.media.AudioFormat;
import android.os.SystemClock;
import android.speech.tts.SynthesisCallback;
import android.speech.tts.SynthesisRequest;
import android.speech.tts.TextToSpeech;
import android.speech.tts.TextToSpeechService;
import android.util.Log;

/**
 * A speech engine that says nothing and writes down what it was asked to
 * say, as `<elapsed ms> <package> <text>` under TAG in the log. Made so
 * huvu could read what Netflix's screens say, which hide from screenshots
 * and from the accessibility tree: VoiceView, Fire TV's screen reader, would
 * read them out into the box's default engine, and with this one the
 * default, nothing would be heard in the room. Not in use (checked on the
 * family's Stick on 2026-11-01): VoiceView speaks only through Amazon's own
 * engines (Ivona, then Pico), whatever the default is, and with VoiceView's
 * text banner on, Netflix gave it nothing to show and spoke into no engine.
 * Nothing on the box makes it the default.
 */
public final class SilentSpeech extends TextToSpeechService {
    static final String TAG = "huvu-said";

    private String[] language = {"eng", "USA", ""};

    @Override
    protected int onIsLanguageAvailable(String lang, String country, String variant) {
        return TextToSpeech.LANG_COUNTRY_AVAILABLE;
    }

    @Override
    protected String[] onGetLanguage() {
        return language;
    }

    @Override
    protected int onLoadLanguage(String lang, String country, String variant) {
        language = new String[] {lang == null ? "eng" : lang, country == null ? "" : country, variant == null ? "" : variant};
        return TextToSpeech.LANG_COUNTRY_AVAILABLE;
    }

    @Override
    protected void onStop() {
        // Nothing plays, so nothing stops.
    }

    @Override
    protected void onSynthesizeText(SynthesisRequest request, SynthesisCallback callback) {
        CharSequence said = request.getCharSequenceText();
        String text = said != null ? said.toString() : request.getText();
        if (text != null && !text.trim().isEmpty()) {
            String caller = getPackageManager().getNameForUid(request.getCallerUid());
            Log.i(TAG, SystemClock.elapsedRealtime() + " " + (caller == null ? "unknown" : caller) + " " + text.replaceAll("\\s+", " ").trim());
        }
        // An utterance with no sound in it: the caller hears it end at once.
        callback.start(16000, AudioFormat.ENCODING_PCM_16BIT, 1);
        callback.done();
    }
}
