import io
import wave

import numpy as np
import pytest

from backend.audio import Segmenter, amplify, mix_to_mono, rms, to_wav


def tone(amplitude=1000, rate=8000, seconds=0.05, frequency=400):
    return np.rint(amplitude * np.sin(2 * np.pi * frequency * np.arange(int(rate * seconds)) / rate)).astype(np.int16)


def read_pcm(wav):
    with wave.open(io.BytesIO(wav)) as stream:
        return np.frombuffer(stream.readframes(stream.getnframes()), dtype='<i2')


@pytest.mark.parametrize('channels,occupied', [(1, [0]), (2, [0]), (2, [0, 1]), (8, [0, 1]), (8, [2])])
def test_surround_and_one_sided_speech_keep_original_level(channels, occupied):
    speech = tone()
    frames = np.zeros((len(speech), channels), dtype=np.int16)
    for index in occupied:
        frames[:, index] = speech
    np.testing.assert_array_equal(mix_to_mono(frames.tobytes(), channels), speech)


def test_opposite_phase_speech_does_not_cancel_and_regular_stereo_keeps_both():
    left = tone()
    right = tone(frequency=600)
    inverted = np.column_stack([left, -left])
    np.testing.assert_array_equal(mix_to_mono(inverted.tobytes(), 2), left)
    stereo = np.column_stack([left, right])
    expected = np.rint(stereo.astype(np.float32).mean(axis=1)).astype(np.int16)
    np.testing.assert_array_equal(mix_to_mono(stereo.tobytes(), 2), expected)


def test_quiet_stereo_in_eight_channels_no_longer_misses_whole_sentence():
    speech = tone(amplitude=650)
    surround = np.zeros((len(speech), 8), dtype=np.int16)
    surround[:, :2] = speech[:, None]
    old = surround.astype(np.float32).mean(axis=1).astype(np.int16)
    new = mix_to_mono(surround.tobytes(), 8)
    assert rms(old) < 0.008 < rms(new)
    results = []
    for pcm in [old, new]:
        detector = Segmenter(8000, silence=0.5)
        segments = []
        for frame in [pcm] * 12 + [np.zeros_like(pcm)] * 12:
            _, segment = detector.feed(frame)
            if segment:
                segments.append(segment)
        results.append(segments)
    assert results[0] == []
    assert len(results[1]) == 1 and results[1][0].final
    assert rms(read_pcm(results[1][0].wav)) > rms(new)


def test_gain_helps_quiet_detection_without_turning_silence_into_speech():
    speech = tone(amplitude=120)
    assert rms(speech) < 0.008 < rms(amplify(speech, 4))
    silent = np.zeros(400, dtype=np.int16)
    np.testing.assert_array_equal(amplify(silent, 16), silent)
    np.testing.assert_array_equal(mix_to_mono(np.zeros((400, 8), dtype=np.int16).tobytes(), 8), silent)


def test_gain_keeps_peak_headroom_without_integer_wrap_or_flat_clipping():
    pcm = np.array([-10000, -5000, 0, 5000, 10000], dtype=np.int16)
    result = amplify(pcm, 16)
    assert result[0] < result[1] < 0 < result[3] < result[4]
    assert np.max(np.abs(result.astype(np.int32))) <= 0.95 * 32767 + 1
    assert result[4] / result[3] == pytest.approx(2, abs=0.001)
    full_scale = np.array([-32768, 32767], dtype=np.int16)
    np.testing.assert_array_equal(amplify(full_scale, 16), full_scale)


def test_asr_normalization_is_bounded_preserves_silence_and_wav_format():
    quiet = tone(amplitude=20)
    output = to_wav([quiet], 8000, normalize=True)
    assert rms(read_pcm(output)) == pytest.approx(rms(quiet) * 8, rel=0.01)
    with wave.open(io.BytesIO(output)) as stream:
        assert (stream.getnchannels(), stream.getsampwidth(), stream.getframerate()) == (1, 2, 8000)
    silent = np.zeros(400, dtype=np.int16)
    assert not np.any(read_pcm(to_wav([silent], 8000, normalize=True)))
    detector = Segmenter(8000)
    for _ in range(100):
        assert detector.feed(quiet)[1] is None


def test_switch_flushes_accepted_speech_but_not_noise_or_finished_turn():
    detector = Segmenter(8000, silence=0.5)
    detector.feed(tone())
    assert detector.finish() is None
    for _ in range(6):
        detector.feed(tone())
    final = detector.finish()
    assert final.final and len(read_pcm(final.wav)) >= 2800
    for _ in range(12):
        detector.feed(np.zeros(400, dtype=np.int16))
    assert detector.finish() is None
