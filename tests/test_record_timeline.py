"""The recorder's WAV writer on top of RtpTimeline, fed fake RTP packets (no scanner)."""
import json
from types import SimpleNamespace

import numpy as np

from scanner_transcriber.audio import read_wav
from scanner_transcriber.tools.record import _WavTimeline


def test_wav_timeline_gap_fill_and_anchor(tmp_path):
    t = _WavTimeline(tmp_path / "a.wav", tmp_path / "anchors.jsonl")
    payload = bytes([0x80]) * 320          # µ-law 0x80 = +32124
    t.on_packet(SimpleNamespace(timestamp=1000, payload=payload))
    t.on_packet(SimpleNamespace(timestamp=1640, payload=payload))  # one packet lost
    t.close()
    pcm, rate = read_wav(tmp_path / "a.wav")
    assert rate == 8000 and len(pcm) == 960
    assert np.all(pcm[:320] == 32124) and not pcm[320:640].any() and np.all(pcm[640:] == 32124)
    assert t.zero_filled == 320 and t.samples_written == 960
    anchors = [json.loads(line) for line in open(tmp_path / "anchors.jsonl")]
    assert anchors[0]["sample"] == 0
