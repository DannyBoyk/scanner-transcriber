import json

import numpy as np

from scanner_transcriber.audio import write_wav
from scanner_transcriber.tools import cut


def _status(mono, tgid, mute):
    return {"mono": mono, "status": {"tgid": tgid, "mute": mute, "system": "S",
                                     "department": "D", "channel": "C", "unit_id": "UID:1"}}


def test_cut_splits_on_silence_and_labels(tmp_path):
    rate = 8000
    pcm = np.zeros(10 * rate, dtype=np.int16)
    pcm[1 * rate:3 * rate] = 1000        # tx 1: 2 s
    pcm[int(3.2 * rate):int(3.5 * rate)] = 1000  # 200 ms gap -> merged into tx 1
    pcm[6 * rate:int(6.2 * rate)] = 1000  # 200 ms click -> dropped
    pcm[8 * rate:9 * rate] = 1000        # tx 2: 1 s
    write_wav(tmp_path / "audio.wav", pcm)
    (tmp_path / "anchors.jsonl").write_text(json.dumps({"mono": 100.0, "sample": 0}) + "\n")
    rows = [_status(101.2, "1101", "Unmute"), _status(104.0, None, "Mute"),
            _status(108.1, "2202", "Unmute")]
    (tmp_path / "status.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))

    report = json.loads(cut.run(tmp_path).read_text())
    clips = [json.loads(line) for line in (tmp_path / "clips.jsonl").open()]

    assert [c["tgid"] for c in clips] == ["1101", "2202"]
    assert clips[0]["duration_ms"] == 2500
    assert report["dropped_short"] == 1
    assert report["unmute_minus_onset_ms_median"] == 150
    assert (tmp_path / "clips" / "0001.wav").exists()
