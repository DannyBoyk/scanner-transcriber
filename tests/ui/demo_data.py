"""Fills a scratch database with a believable but invented hour of scanner traffic, for the
README screenshots (tests/ui/screenshots.sh). Talkgroups, units and timing look like the real
thing; every transcript and address is made up, so no real dispatch traffic is published.

    demo_data.py DATA_DIR
"""
from __future__ import annotations

import json
import sys
import wave
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np

from scanner_transcriber import auth, db

SYSTEM = "County Trunked System"
SITE = "County Simulcast"
# (tgid, department, channel, colour index, alias)
CHANNELS = [
    ("2101", "County Sheriff", "Dispatch", 0, None),
    ("2302", "County Fire/Rescue", "Fire Response", 1, None),
    ("2201", "Town Police", "Dispatch", 2, None),
    ("2301", "County Fire/Rescue", "Fire Dispatch", 3, None),
    ("2350", "County Fire/Rescue", "Hospital", 4, "ER Hospital"),
    ("2501", "County Government", "Public Works", 5, None),
    ("2303", "County Fire/Rescue", "Incident 1", 6, None),
    ("2601", "Other", "Medevac Dispatch", 7, None),
]

# (seconds before now it started, tgid, unit, transcript); oldest first. Invented.
TRAFFIC = [
    (3900, "2101", 4012, "County, 142, I'll be out on a traffic stop, Route 12 southbound at the Hillcrest overpass."),
    (3880, "2101", 1, "142, copy, Route 12 southbound at Hillcrest."),
    (3700, "2201", 2210, "Town, 21, show me 10-8."),
    (3690, "2201", 2, "21, 10-8, {t}."),
    (3400, "2301", 3, "Station 4 and Medic 7, respond for a 74-year-old female, fall with a possible hip injury, 1180 Meadowbrook Lane, cross street Pine Ridge Road."),
    (3360, "2302", 3471, "Medic 7, responding."),
    (3350, "2302", 3, "Medic 7 responding, {t}."),
    (3100, "2101", 4012, "142, clear the stop, one in custody for an outstanding warrant, en route to the jail."),
    (3080, "2101", 1, "Copy, 142, en route to the jail with one, mileage when you have it."),
    (2900, "2302", 3471, "Medic 7 on scene."),
    (2700, "2501", 5120, "Public Works, unit 12, we've got a tree down across Mill Creek Road about a mile past the bridge. Need a second crew with a chipper."),
    (2690, "2501", 5101, "Copy unit 12, I'll send crew 3 your way."),
    (2400, "2350", 3471, "ER, Medic 7, how do you copy?"),
    (2392, "2350", 6001, "Medic 7, ER, go ahead."),
    (2380, "2350", 3471, "Medic 7 transporting one, 74-year-old female, ground-level fall, suspected left hip fracture, vitals stable, pain 7 out of 10. ETA 15 minutes. Any questions?"),
    (2360, "2350", 6001, "No questions, see you in 15, room 4."),
    (2200, "2201", 2210, "21, Town, can you check on a vehicle parked in the lot behind the library? Caller says it's been there since last night with the engine running."),
    (2190, "2201", 2210, "21, copy, en route."),
    (1900, "2301", 3, "Station 1, Station 4, Engine 1, Tower 1, respond for a commercial fire alarm, 300 Main Street, the hardware store, zone 3 smoke detector."),
    (1880, "2302", 3101, "Engine 1, responding with four."),
    (1870, "2302", 3105, "Tower 1 responding."),
    (1700, "2302", 3101, "Engine 1 on scene, nothing showing, we'll be investigating."),
    (1690, "2302", 3, "Engine 1 on scene, nothing showing, 12:58."),
    (1500, "2201", 2210, "21, the vehicle's unoccupied, owner's on the way. Clear."),
    (1480, "2201", 2, "21, direct."),
    (1300, "2302", 3101, "Engine 1, Tower 1, you can return. Burnt popcorn in the break room. We'll reset the panel and be clear."),
    (1290, "2302", 3105, "Tower 1 returning."),
    (1100, "2601", 7001, "Medevac 2 is in service at the hospital, available for calls."),
    (900, "2101", 4033, "County, 155, I'll be at the high school for the football game, Stadium Drive."),
    (890, "2101", 1, "155, high school, {t}."),
    (700, "2301", 3, "Station 6 and Medic 6, respond for a motor vehicle crash with injuries, Route 40 northbound near mile marker 12, two vehicles, one with airbag deployment."),
    (680, "2302", 3611, "Engine 6, responding with three."),
    (670, "2302", 3612, "Medic 6, responding."),
    (660, "2101", 1, "Any unit near Route 40 mile marker 12 for a crash with injuries? Fire and rescue are en route."),
    (650, "2101", 4051, "168, I'm about two minutes out."),
    (480, "2303", 3611, "Engine 6 on scene, two vehicles, moderate damage. We've got one patient complaining of neck pain. Setting up a block in the right lane."),
    (470, "2303", 3, "Engine 6 on scene, one patient, {t}."),
    (400, "2101", 4051, "168, on scene with fire. Let's get a wrecker started for one vehicle."),
    (390, "2101", 1, "168, copy, one wrecker. Rotation is Smith's Towing."),
    (300, "2303", 3612, "Medic 6, we'll be transporting one to the ER, stable. Engine 6 can handle the rest."),
    (240, "2201", 2214, "Town, 24, I'll be out with a disabled vehicle on Broad Street at the light, no traffic hazard."),
    (230, "2201", 2, "24, copy."),
    (150, "2350", 3612, "ER, Medic 6, we'll be en route to you with a 30-year-old male, restrained driver, neck pain, full spinal precautions, ETA 20."),
    (140, "2350", 6001, "Medic 6, copy, room 2."),
    (80, "2303", 3611, "Engine 6, the roadway is open, wrecker's on scene. We're returning."),
    (40, "2101", 4051, "168, I'll be clear, wrecker has the vehicle."),
    (30, "2101", 1, "168, clear, {t}."),
]

# a few from earlier days, so search has history
HISTORY = [
    (1, "2302", 3101, "Engine 1 on scene, smoke showing from the second floor, establishing Main Street command."),
    (1, "2301", 3, "Station 1, Station 4, respond for a residential structure fire, 22 Elm Street, caller reports smoke from the attic."),
    (2, "2501", 5120, "Unit 12 to Public Works, the water main on Main Street is repaired, road is open."),
    (3, "2201", 2210, "21, I'll be out on Main Street on foot for the parade detail."),
]


def wav(path: Path, seconds: float, seed: int) -> None:
    """Speech-like noise: band-limited, syllable-rate envelope, at scanner level (-30 dBFS)."""
    rng = np.random.default_rng(seed)
    n = int(8000 * seconds)
    x = np.convolve(rng.standard_normal(n), np.ones(6) / 6, mode="same")
    t = np.arange(n) / 8000
    env = np.clip(np.sin(2 * np.pi * 3.5 * t + rng.uniform(0, 6)), 0, None) ** 0.5
    x = x * env
    x = x / (np.sqrt(np.mean(x ** 2)) + 1e-9) * 0.03 * 32767
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(x.astype("<i2").tobytes())


def main(data_dir: str) -> None:
    data = Path(data_dir)
    conn = db.connect(data / "scanner.db")
    db.migrate(conn)
    now = datetime.now(UTC).replace(microsecond=0)
    iso = lambda t: t.isoformat(timespec="milliseconds")
    for tg, dep, ch, color, alias in CHANNELS:
        conn.execute("INSERT OR REPLACE INTO channels (key, system, department, channel, tgid, alias, "
                     "color, first_heard_at, last_heard_at) VALUES (?,?,?,?,?,?,?,?,?)",
                     (f"sys:{SYSTEM}|tg:{tg}", SYSTEM, dep, ch, tg, alias, color,
                      iso(now - timedelta(days=4)), iso(now)))
    names = {tg: (dep, ch) for tg, dep, ch, _, _ in CHANNELS}
    rows = [(now - timedelta(seconds=s), tg, u, text) for s, tg, u, text in TRAFFIC]
    rows += [(now - timedelta(days=d, hours=2), tg, u, text) for d, tg, u, text in HISTORY]
    rows.sort()
    for i, (start, tg, unit, text) in enumerate(rows):
        dur = max(1.2, len(text.split()) / 2.6)
        end = start + timedelta(seconds=dur)
        done = end + timedelta(seconds=5.5 + (i % 5) * 0.6)
        dep, ch = names[tg]
        text = text.replace("{t}", f"{start.astimezone():%H:%M}")   # dispatchers read out the time
        rel = f"clips/{start:%Y/%m/%d/%H%M%S}_{i + 1}.wav"
        wav(data / rel, dur, i)
        conn.execute(
            "INSERT INTO transmissions (id, channel_key, started_at, ended_at, duration_ms, system, "
            "department, channel, tgid, unit_id, unit_ids, site, frequency_hz, modulation, rssi, "
            "audio_path, audio_format, status, attempts, transcript, raw_transcript, flags, "
            "stt_backend, stt_model, stt_ms, created_at, updated_at, transcribed_at, prompt_version) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (i + 1, f"sys:{SYSTEM}|tg:{tg}", iso(start), iso(end), int(dur * 1000), SYSTEM, dep,
             ch, tg, f"UID:{unit}", json.dumps([f"UID:{unit}"]), SITE, 856_237_500, "NFM", -62,
             rel, "wav", "done", 1, text, text, "[]", "ovwhisper",
             "OpenVINO/whisper-small.en-int8-ov", int(dur * 700), iso(end), iso(done), iso(done), 1))
    conn.execute("INSERT INTO transmissions_tri(transmissions_tri) VALUES ('rebuild')")
    prompt = ("County Sheriff, Town Police, Fire and Rescue dispatch. Medic 7, Engine 1, Tower 1, "
              "Station 4. 10-4, 10-8, direct, copy, en route, on scene, Route 12, Route 40.")
    conn.execute("INSERT INTO prompt_versions (id, kind, body, note, created_at, activated_at) "
                 "VALUES (1, 'prompt', ?, 'Unit names and local roads', ?, ?)",
                 (prompt, iso(now - timedelta(days=3)), iso(now - timedelta(days=3))))
    conn.execute("INSERT INTO prompt_versions (id, kind, body, note, created_at, activated_at) "
                 "VALUES (2, 'substitutions', ?, NULL, ?, ?)",
                 (json.dumps({"10 4": "10-4", "10 8": "10-8", "medic seven": "Medic 7"}),
                  iso(now - timedelta(days=3)), iso(now - timedelta(days=3))))
    conn.execute("INSERT OR REPLACE INTO active_versions VALUES ('prompt', 1), ('substitutions', 2)")
    for ago, comp, kind, msg in [(86400 * 2, "capture", "started", "Capture started"),
                                 (86400 * 2 - 2, "capture", "connected", "Scanner connected"),
                                 (86400 * 2 - 1, "worker", "started", "Worker started on the GPU")]:
        conn.execute("INSERT INTO system_events (at, component, level, kind, message) VALUES (?,?,?,?,?)",
                     (iso(now - timedelta(seconds=ago)), comp, "info", kind, msg))
    auth.set_password(conn, "demo-pass-123")
    conn.commit()
    print(f"{len(rows)} demo transmissions in {data}")


if __name__ == "__main__":
    main(sys.argv[1])
