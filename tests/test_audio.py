import numpy as np

from scanner_transcriber.audio import ULAW_TABLE, read_wav, resample, ulaw_to_pcm16, write_wav


def test_ulaw_known_values():
    # G.711: 0xFF and 0x7F are the two zeros; 0x00 / 0x80 are the extremes.
    assert ULAW_TABLE[0xFF] == 0
    assert ULAW_TABLE[0x7F] == 0
    assert ULAW_TABLE[0x00] == -32124
    assert ULAW_TABLE[0x80] == 32124


def test_ulaw_is_monotonic_per_sign():
    pos = ULAW_TABLE[0x80:0x100]  # 0x80 (max) .. 0xFF (zero)
    assert np.all(np.diff(pos.astype(int)) < 0)


def test_ulaw_decode_bytes():
    out = ulaw_to_pcm16(bytes([0xFF, 0x80, 0x00]))
    assert out.dtype == np.int16
    assert out.tolist() == [0, 32124, -32124]


def test_resample_doubles_length_and_preserves_tone():
    t = np.arange(8000) / 8000
    tone = (0.5 * 32767 * np.sin(2 * np.pi * 1000 * t)).astype(np.int16)
    out = resample(tone)
    assert out.dtype == np.float32
    assert abs(len(out) - 16000) <= 2
    spectrum = np.abs(np.fft.rfft(out))
    assert abs(np.argmax(spectrum) * 16000 / len(out) - 1000) < 5


def test_wav_roundtrip(tmp_path):
    pcm = (np.arange(800) - 400).astype(np.int16)
    write_wav(tmp_path / "a.wav", pcm)
    back, rate = read_wav(tmp_path / "a.wav")
    assert rate == 8000
    assert np.array_equal(back, pcm)


def test_level_brings_quiet_speech_to_target_without_clipping():
    import numpy as np

    from scanner_transcriber.audio import LEVEL_TARGET_DBFS, level
    rng = np.random.default_rng(1)
    quiet = (rng.normal(0, 0.03, 8000) * 32767).astype(np.int16)        # about -30 dBFS
    padded = np.concatenate([np.zeros(4000, np.int16), quiet, np.zeros(4000, np.int16)])
    y = level(padded)
    v = y[y != 0].astype(float) / 32768
    rms = 20 * np.log10(np.sqrt(np.mean(v ** 2)))
    assert abs(rms - LEVEL_TARGET_DBFS) < 1.5                             # padding ignored
    assert np.max(np.abs(y)) <= int(32767 * 10 ** (-1 / 20)) + 1          # never above -1 dBFS
    assert not np.any(y[:4000]) and not np.any(y[-4000:])                 # silence stays silent


def test_level_caps_the_gain_and_leaves_silence_alone():
    import numpy as np

    from scanner_transcriber.audio import LEVEL_MAX_GAIN_DB, level
    faint = np.full(1000, 10, np.int16)                                   # about -70 dBFS
    assert np.max(level(faint)) <= round(10 * 10 ** (LEVEL_MAX_GAIN_DB / 20)) + 1
    assert not np.any(level(np.zeros(500, np.int16)))
