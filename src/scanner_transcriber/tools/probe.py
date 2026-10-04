"""UDP-only probe of the scanner: model, firmware, and raw status payloads.

Never touches RTSP/554, so it is always safe to run (design R2).
"""
from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from pathlib import Path

from sds200 import SDSScanner

from ..config import Config


def run(cfg: Config, *, psi_seconds: float = 10.0) -> Path:
    out_dir = cfg.storage.data_dir / "probe" / datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out_dir.mkdir(parents=True, exist_ok=True)

    with SDSScanner.network(cfg.scanner.host, remote_port=cfg.scanner.control_port) as scanner:
        info = {"model": str(scanner.get_model()), "firmware": scanner.get_firmware()}
        print(f"model={info['model']} firmware={info['firmware']}")

        gsi = scanner.get_scanner_info()
        (out_dir / "gsi.xml").write_text(gsi.raw_xml)
        print(f"GSI mode={gsi.mode} screen={gsi.screen} nodes={sorted(gsi.nodes)}")

        sts = scanner.get_status()
        (out_dir / "sts.txt").write_text(sts.packet.raw)

        # Push stream: capture a few seconds of PSI to see how often it fires
        # and what changes when a transmission starts/stops.
        psi_path = out_dir / "psi.jsonl"
        count = 0
        with psi_path.open("w") as f:
            def on_psi(msg):
                nonlocal count
                if not hasattr(msg, "raw_xml"):
                    return  # PSI ack packet
                count += 1
                f.write(json.dumps({"mono": time.monotonic(), "wall": msg.received_at.isoformat(),
                                    "xml": msg.raw_xml}) + "\n")

            unsubscribe = scanner.on_psi(on_psi)
            try:
                scanner.start_scanner_info_push(cfg.scanner.psi_interval_ms)
                time.sleep(psi_seconds)
            finally:
                unsubscribe()
                scanner.stop_scanner_info_push()
        info["psi_messages"] = count
        info["psi_seconds"] = psi_seconds

    (out_dir / "info.json").write_text(json.dumps(info, indent=2))
    print(f"PSI messages in {psi_seconds:.0f}s: {count}")
    print(f"wrote {out_dir}")
    return out_dir
