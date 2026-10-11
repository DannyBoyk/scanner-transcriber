from dataclasses import replace

from segsim import RATE, T0, Sim, run

from scanner_transcriber.config import SegmenterConfig
from scanner_transcriber.segmenter import Segmenter

CFG = SegmenterConfig()


def secs(clip):
    return clip.start_sample / RATE, clip.end_sample / RATE


def test_single_ptt_trimmed_with_padding():
    sim = Sim(6).status(0.2, "Mute").ptt(1.0, 3.0, "1101", unit="UID:1")
    [c] = run(Segmenter(), sim)
    start, end = secs(c)
    assert abs(start - 0.9) < 0.01 and abs(end - 3.1) < 0.01  # 100 ms pad each side
    assert c.status.tgid == "1101" and c.unit_ids == ["UID:1"]
    assert not c.continued and not c.status_missing
    assert abs((c.started_at - T0).total_seconds() - 0.9) < 0.01


def test_preroll_keeps_speech_before_unmute_status():
    sim = Sim(5).status(0.2, "Mute").ptt(1.0, 2.0, "1101", lag=0.25)
    [c] = run(Segmenter(), sim)
    assert secs(c)[0] < 1.0  # speech started 250 ms before the status said Unmute


def test_psi_flicker_inside_one_ptt_stays_one_clip():
    sim = (Sim(6).status(0.2, "Mute").talk(1.0, 4.0)
           .status(1.1, "Unmute", "1101").status(2.0, "Mute", "1101")
           .status(2.25, "Unmute", "1101").status(4.1, "Mute", "1101"))
    [c] = run(Segmenter(), sim)
    assert abs(secs(c)[1] - 4.1) < 0.01


def test_quick_reply_from_other_unit_is_separate_clip():
    sim = (Sim(8).status(0.2, "Mute")
           .ptt(1.0, 2.5, "1101", unit="UID:1")
           .ptt(3.5, 5.0, "1101", unit="UID:2"))  # 1 s later, same talkgroup
    a, b = run(Segmenter(), sim)
    assert a.unit_ids == ["UID:1"] and b.unit_ids == ["UID:2"]


def test_talkgroup_change_without_mute_splits():
    sim = (Sim(6).status(0.2, "Mute").talk(1.0, 2.0).talk(2.05, 3.5)
           .status(1.05, "Unmute", "1101").status(2.05, "Unmute", "1201")
           .status(3.6, "Mute", "1201"))
    a, b = run(Segmenter(), sim)
    assert (a.status.tgid, b.status.tgid) == ("1101", "1201")
    assert secs(a)[1] <= secs(b)[0] + 0.001


def test_long_transmission_split_and_flagged_continued():
    cfg = replace(CFG, max_clip_s=10)
    sim = Sim(30).status(0.2, "Mute").ptt(1.0, 24.0, "1101")
    clips = run(Segmenter(cfg), sim)
    assert len(clips) == 3
    assert [c.continued for c in clips] == [False, True, True]
    assert all(c.status.tgid == "1101" for c in clips)
    total = sum(len(c.pcm) for c in clips)
    assert abs(total / RATE - 23.2) < 0.5  # nothing lost across splits (+ pads)


def test_short_click_dropped():
    sim = Sim(4).status(0.2, "Mute").ptt(1.0, 1.2, "1101")
    assert run(Segmenter(), sim) == []


def test_unmute_with_only_silence_dropped():
    sim = Sim(4).status(0.2, "Mute").status(1.0, "Unmute", "1101").status(2.0, "Mute", "1101")
    assert run(Segmenter(), sim) == []


def test_stale_status_falls_back_to_audio_runs():
    # one early status, then silence from PSI; two separate bursts of audio
    sim = Sim(12).status(0.1, "Mute", "1101").talk(4.0, 5.0).talk(7.0, 8.5)
    clips = run(Segmenter(), sim)
    assert len(clips) == 2
    assert all(c.status_missing for c in clips)


def test_status_resuming_mid_clip_adopts_label():
    sim = Sim(8).talk(1.0, 4.0).status(2.0, "Unmute", "1101").status(4.1, "Mute", "1101")
    [c] = run(Segmenter(), sim)
    assert c.status.tgid == "1101"
    assert not c.status_missing
    assert secs(c)[0] < 1.0


def test_latency_offset_delays_status():
    cfg = replace(CFG, audio_latency_ms=500)
    # status arrives 400 ms *before* the audio (audio is delayed); latency realigns it
    sim = (Sim(6).status(0.1, "Mute").talk(1.5, 3.0)
           .status(1.1, "Unmute", "1101").status(2.6, "Mute", "1101"))
    [c] = run(Segmenter(cfg), sim)
    assert abs(secs(c)[1] - 3.1) < 0.05  # not cut at 2.6 s


def test_flush_emits_open_clip():
    sim = Sim(4).status(0.2, "Mute").talk(1.0, 4.0).status(1.1, "Unmute", "1101")
    assert run(Segmenter(), sim, flush=False) == []
    seg = Segmenter()
    [c] = run(seg, sim)
    assert c.status.tgid == "1101"


def test_status_changes_recorded():
    sim = (Sim(6).status(0.2, "Mute").talk(1.0, 4.0)
           .status(1.1, "Unmute", "1101", unit_id="UID:1")
           .status(2.0, "Unmute", "1101", unit_id="UID:2")
           .status(4.1, "Mute", "1101"))
    [c] = run(Segmenter(), sim)
    assert c.unit_ids == ["UID:1", "UID:2"]
    assert [ch["fields"]["unit_id"] for ch in c.status_changes] == ["UID:1", "UID:2", None]


def test_unit_handoff_without_mute_splits_at_silence():
    # scanner stays unmuted from unit 1's PTT into unit 2's reply (seen live, clip 0101)
    sim = (Sim(8).status(0.2, "Mute").talk(1.0, 3.5).talk(4.3, 5.5)
           .hold(1.1, 4.6, "Unmute", "1101", unit_id="UID:1")
           .hold(4.6, 5.6, "Unmute", "1101", unit_id="UID:2")
           .hold(5.6, 8.0, "Mute", "1101"))
    a, b = run(Segmenter(), sim)
    assert a.unit_ids == ["UID:1"] and b.unit_ids == ["UID:2"]
    assert abs(secs(a)[1] - 3.6) < 0.02   # first part ends at its speech (+pad)
    assert abs(secs(b)[0] - 4.2) < 0.02   # reply starts where the silence ends (-pad)
    assert b.status.unit_id == "UID:2"


def test_stale_unit_at_clip_start_does_not_split():
    # status briefly shows the previous unit, then the real talker; no split, no lost audio
    sim = (Sim(6).status(0.2, "Mute").talk(1.0, 3.0)
           .status(1.05, "Unmute", "1101", unit_id="UID:old")
           .status(1.2, "Unmute", "1101", unit_id="UID:new")
           .status(3.1, "Mute", "1101"))
    [c] = run(Segmenter(), sim)
    assert c.unit_ids == ["UID:old", "UID:new"]
    assert abs(secs(c)[0] - 0.9) < 0.02


def test_unit_split_can_be_disabled():
    cfg = replace(CFG, split_on_unit_change=False)
    sim = (Sim(8).status(0.2, "Mute").talk(1.0, 3.5).talk(4.3, 5.5)
           .hold(1.1, 4.6, "Unmute", "1101", unit_id="UID:1")
           .hold(4.6, 5.6, "Unmute", "1101", unit_id="UID:2")
           .hold(5.6, 8.0, "Mute", "1101"))
    [c] = run(Segmenter(cfg), sim)
    assert c.unit_ids == ["UID:1", "UID:2"]


def test_pending_statuses_are_bounded_when_audio_stops():
    # Statuses wait for the audio clock; if audio stops they must not pile up without limit.
    from datetime import timedelta

    from scanner_transcriber.models import ScannerStatus
    from scanner_transcriber.segmenter import MAX_PENDING_STATUSES

    seg = Segmenter(CFG)
    for i in range(1000):
        seg.feed_status(ScannerStatus(mono=float(i), wall=T0 + timedelta(seconds=i),
                                      sample=float(RATE * (i + 1)), mute="Mute"))
    assert len(seg._pending) == MAX_PENDING_STATUSES
