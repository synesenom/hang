"""Speaker verification: only the trained voice gets through.

A small local model (WeSpeaker ResNet34 via sherpa-onnx) turns a clip into a
256-number voiceprint. A clip is accepted when its cosine similarity to the
trained voiceprint reaches the threshold saved by `hang train`.

To estimate how well that separates you from other people, training also scores
a handful of built-in macOS voices ("comparison voices") against your voiceprint.

Everything is optional: without the Poetry environment, the model or a
voiceprint, `Verifier.load()` returns None and every clip is accepted.
"""
import json
import os
import subprocess
import tempfile
import time
import wave

from hang import ROOT

MODEL = os.environ.get("HANG_SPEAKER_MODEL",
                       os.path.join(ROOT, "models", "wespeaker_en_voxceleb_resnet34.onnx"))
VOICEPRINT = os.environ.get("HANG_VOICEPRINT", os.path.join(ROOT, "models", "voiceprint.json"))
COMPARISON_CACHE = os.environ.get("HANG_COMPARISON_CACHE", os.path.join(ROOT, "models", "comparison-voices.json"))
RATE = 16000

# Built-in macOS voices that stand in for "other people" (novelty voices left out).
COMPARISON_VOICES = os.environ.get("HANG_COMPARISON_VOICES", "").split(",") if os.environ.get(
    "HANG_COMPARISON_VOICES") else ["Samantha", "Daniel", "Karen", "Moira", "Rishi", "Tessa", "Fred", "Albert",
                                  "Kathy", "Ralph", "Junior", "Flo", "Reed", "Sandy", "Shelley", "Eddy"]
COMPARISON_TEXT = "Please list the files in this folder, then run the tests and tell me what failed."


def extractor():
    import sherpa_onnx
    return sherpa_onnx.SpeakerEmbeddingExtractor(
        sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=MODEL, num_threads=1))


def available():
    """None if speaker verification can run, else why not."""
    try:
        import numpy  # noqa: F401
        import sherpa_onnx  # noqa: F401
    except ImportError:
        return "sherpa-onnx is not installed (run: poetry install)"
    if not os.path.exists(MODEL):
        return "the speaker model is missing (run ./setup.sh)"
    return None


def read_wav(path):
    import numpy as np
    with wave.open(path) as w:
        pcm = w.readframes(w.getnframes())
    return np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0


def trim(samples, frame=480, rel=0.1, pad=3):
    """Drop the quiet start and end of a clip. A live utterance carries 0.3 s
    before onset and a full pause after it: on a short phrase ("Claude, hang")
    that is more silence than speech, and the voice score drops below the
    threshold. Keeps frames from the first to the last one louder than `rel`
    of the loudest, plus `pad` frames (90 ms) either side."""
    import numpy as np
    n = len(samples) // frame
    if n < 2:
        return samples
    rms = np.sqrt(np.mean(samples[:n * frame].reshape(n, frame) ** 2, axis=1))
    loud = np.nonzero(rms >= rel * rms.max())[0]
    if not len(loud):
        return samples
    a, b = max(0, loud[0] - pad), min(n, loud[-1] + 1 + pad)
    return samples[a * frame:b * frame]


def embed(ex, samples):
    import numpy as np
    stream = ex.create_stream()
    stream.accept_waveform(sample_rate=RATE, waveform=samples)
    stream.input_finished()
    e = np.array(ex.compute(stream), dtype=np.float32)
    return e / (np.linalg.norm(e) or 1.0)


def unit(v):
    import numpy as np
    return v / (np.linalg.norm(v) or 1.0)


def comparison_embeddings(ex, on_voice=lambda name: None):
    """Voiceprints of the built-in comparison voices, cached in models/."""
    import numpy as np
    try:
        with open(COMPARISON_CACHE) as f:
            data = json.load(f)
        if data.get("model") == os.path.basename(MODEL) and data.get("voices"):
            return {k: np.array(v, dtype=np.float32) for k, v in data["voices"].items()}
    except (OSError, ValueError):
        pass
    try:
        installed = subprocess.run(["say", "-v", "?"], capture_output=True, text=True).stdout
    except OSError:
        return {}
    names = [v for v in COMPARISON_VOICES if any(line.startswith(v + " ") for line in installed.splitlines())]
    voices = {}
    tmp = tempfile.mkdtemp(prefix="vc-compare-")
    try:
        for name in names:
            on_voice(name)
            aiff, wav = os.path.join(tmp, "v.aiff"), os.path.join(tmp, "v.wav")
            r = subprocess.run(["say", "-v", name, "-o", aiff, COMPARISON_TEXT], capture_output=True)
            if r.returncode != 0:
                continue
            subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-i", aiff, "-ar", str(RATE), "-ac", "1", wav],
                           check=True)
            voices[name] = embed(ex, trim(read_wav(wav)))
    finally:
        for f in os.listdir(tmp):
            os.unlink(os.path.join(tmp, f))
        os.rmdir(tmp)
    try:
        with open(COMPARISON_CACHE, "w") as f:
            json.dump({"model": os.path.basename(MODEL),
                       "voices": {k: [float(x) for x in v] for k, v in voices.items()}}, f)
    except OSError:
        pass
    return voices


def choose_threshold(own_scores, other_scores):
    """Between your weakest clip and the most similar other voice, leaning
    toward accepting you; clamped to a sane range."""
    lo = max(other_scores) + 0.05 if other_scores else 0.45
    hi = min(own_scores) - 0.05 if own_scores else 0.7
    t = (lo + hi) / 2 if hi >= lo else (min(own_scores) + max(other_scores)) / 2 if other_scores else hi
    return round(min(0.75, max(0.35, t)), 3)


def save(embedding, threshold, own_scores, accuracy):
    data = {"model": os.path.basename(MODEL), "threshold": threshold, "trained": time.strftime("%Y-%m-%d %H:%M"),
            "self_scores": [round(float(x), 3) for x in own_scores], "accuracy": accuracy,
            "embedding": [float(x) for x in embedding]}
    os.makedirs(os.path.dirname(VOICEPRINT), exist_ok=True)
    with open(VOICEPRINT, "w") as f:
        json.dump(data, f)
    return data


def info():
    """The saved voiceprint's summary (no embedding), or None."""
    try:
        with open(VOICEPRINT) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    data.pop("embedding", None)
    return data


def forget():
    if os.path.exists(VOICEPRINT):
        os.unlink(VOICEPRINT)
        return True
    return False


class Verifier:
    def __init__(self, ex, voiceprint, threshold):
        self.extractor = ex
        self.voiceprint = voiceprint
        self.threshold = threshold

    @classmethod
    def load(cls, log=lambda msg: None):
        if not os.path.exists(VOICEPRINT):
            return None
        problem = available()
        if problem:
            log("speaker check off: %s" % problem)
            return None
        try:
            import numpy as np
            with open(VOICEPRINT) as f:
                data = json.load(f)
            threshold = float(os.environ.get("HANG_SPEAKER_THRESHOLD", data["threshold"]))
            v = cls(extractor(), np.array(data["embedding"], dtype=np.float32), threshold)
        except Exception as e:  # bad file, broken model...
            log("speaker check off: %s" % e)
            return None
        log("speaker check on (threshold %.2f)" % threshold)
        return v

    def score(self, wav_path):
        return float(embed(self.extractor, trim(read_wav(wav_path))) @ self.voiceprint)

    def accepts(self, wav_path):
        s = self.score(wav_path)
        return s >= self.threshold, s
