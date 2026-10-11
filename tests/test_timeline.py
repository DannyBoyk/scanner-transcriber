import numpy as np

from scanner_transcriber.timeline import AnchorClock, RtpTimeline


def pkt(n=320, value=100):
    return np.full(n, value, dtype=np.int16)


def test_contiguous_packets():
    t = RtpTimeline()
    assert t.place(1000, pkt())[0] == 0
    start, pcm = t.place(1320, pkt())
    assert start == 320 and len(pcm) == 320
    assert t.samples == 640 and t.zero_filled == 0


def test_lost_packet_is_zero_filled():
    t = RtpTimeline()
    t.place(0, pkt())
    start, pcm = t.place(640, pkt())  # one 320-sample packet missing
    assert start == 320
    assert len(pcm) == 640 and not pcm[:320].any() and pcm[320:].all()
    assert t.zero_filled == 320


def test_duplicate_overlap_is_dropped():
    t = RtpTimeline()
    t.place(0, pkt())
    _, pcm = t.place(0, pkt())
    assert len(pcm) == 0 and t.samples == 320


def test_timestamp_wraps_32_bits():
    t = RtpTimeline()
    t.place(0xFFFFFFFF - 159, pkt())
    start, pcm = t.place(160, pkt())  # wrapped past zero, contiguous
    assert start == 320 and len(pcm) == 320 and t.resets == 0


def test_backwards_jump_is_reset_not_huge_gap():
    t = RtpTimeline()
    t.place(50_000, pkt())
    start, pcm = t.place(10, pkt())
    assert t.resets == 1 and start == 320 and len(pcm) == 320


def test_anchor_clock_roundtrip():
    c = AnchorClock([(100.0, 0), (101.0, 8000), (102.0, 16010)])
    assert c.to_sample(100.5) == 4000
    assert c.to_sample(102.0) == 16010
    assert abs(c.to_mono(12000) - 101.5) < 1e-9


def test_keeps_placing_audio_after_2_pow_32_samples():
    # Regression: positions were measured from the first timestamp, so after 2^32 samples
    # (6.2 days at 8 kHz) every packet looked like a repeat and the audio silently stopped.
    t = RtpTimeline()
    base = 12345
    t.place(base, pkt())
    # fast-forward ~6.2 days of contiguous audio without allocating it
    t.samples, t._last_pos = 2**32, 2**32 - 320
    t._last_rtp = (base + 2**32 - 320) & 0xFFFFFFFF
    for i in range(4):
        start, pcm = t.place((base + 2**32 + i * 320) & 0xFFFFFFFF, pkt())
        assert start == 2**32 + i * 320 and len(pcm) == 320
    assert t.resets == 0 and t.zero_filled == 0
