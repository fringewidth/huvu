# The wake word model

"hey huvu" is heard on the Android app by openWakeWord, ported to Java (`frontend/android/app/src/main/java/com/huvu/app/wake/`). It needs three models in `frontend/android/app/src/main/assets/wakeword/`:

1. `melspectrogram.onnx` and `embedding_model.onnx`: openWakeWord's own feature models, from its v0.5.1 release. Every wake word uses the same two, and they are in the repo.
1. `hey_huvu.onnx`: the model for the phrase itself. It is trained on the dev machine, from Piper's synthetic speech and the family's own recordings. Until it is in the assets, the menu's "turn on" says the build has no model.

openWakeWord's models are licensed CC BY-NC-SA 4.0. That is fine for the family. It would not be fine for a product.

## Training it

It trains on the dev machine, an M4 Pro: the model is small, and a GPU makes little difference. Everything it downloads and makes goes to `wakeword/data/`, which git ignores.

1. **Piper's speech.** Thousands of clips of the phrase from Piper's `libritts_r` voices, about 900 speakers, spelled as the family says it ("hey hoo voo"; huvu's own voice says its name that way, `SAID_NAME` in `backend/src/huvu/speech.py`). Also phrases that sound close, as negatives.
1. **The family's own voices.** Piper has no Indian English voice. Each person records themselves on the screen huvu opens when asked to learn their voice. It walks them through about ten minutes of steps, and keeps each clip on the host, at `/opt/data/huvu/data/voice_clips/<user>/`. A part of each person's clips is held back, to measure the model on real voices.
1. **What must not wake it.** openWakeWord's features of 2,000 hours of ordinary audio (ACAV100M, 17.3 GB), its validation set, and each person's near-misses and talk.

The training script is not written yet.

## Tuning it

Test it on the phone, in the rooms where it will be used.

1. It misses "hey huvu" too often: record more of your own clips, especially in the rooms where it misses, and train again.
1. It wakes when nobody said it: raise `THRESHOLD` in `WakeService.java` from 0.5 to 0.6 or 0.7.

`adb logcat -s huvu.wake` shows each time it heard the phrase, with the score.
