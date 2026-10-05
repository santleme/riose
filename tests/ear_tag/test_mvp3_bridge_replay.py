from __future__ import annotations

import binascii
import json
import shutil
from types import SimpleNamespace

from riose.products.ear_tag.mvp3.bridge import replay_in_renode


def _sx1262_trace() -> dict[str, object]:
    packet = bytearray(24)
    packet[0] = 1
    packet[1] = 3
    packet[2:6] = (1).to_bytes(4, "little")
    packet[6:10] = (0).to_bytes(4, "little")
    packet[10:14] = (0).to_bytes(4, "little")
    packet[22:24] = binascii.crc_hqx(packet[:22], 0xFFFF).to_bytes(2, "little")
    later_packet = bytearray(packet)
    later_packet[10:14] = (300).to_bytes(4, "little")
    later_packet[22:24] = binascii.crc_hqx(later_packet[:22], 0xFFFF).to_bytes(2, "little")
    return {
        "schema_version": "riose.renode.sx1262_tx_trace/v1",
        "clock": "Machine.ElapsedVirtualTime.TimeElapsed",
        "time_unit": "ns",
        "provenance": "SIMULATED",
        "records": [{
            "tx_index": 1, "tx_start_ns": 8_158_370, "tx_done_ns": 38_158_370,
            "payload_length": 24, "payload_hex": packet.hex(), "payload_captured": True,
            "rf_frequency_word": 0x39300000, "tx_power_dbm": 10,
            "status": "completed",
        }, {
            "tx_index": 2, "tx_start_ns": 327_153_870, "tx_done_ns": 357_153_870,
            "payload_length": 24, "payload_hex": later_packet.hex(), "payload_captured": True,
            "rf_frequency_word": 0x39300000, "tx_power_dbm": 10,
            "status": "completed",
        }],
    }


def test_renode_replay_records_gazebo_derived_wakeup_tx(tmp_path, monkeypatch) -> None:
    experiment = tmp_path / "walking"
    experiment.mkdir()
    (experiment / "gazebo_imu.resd").write_bytes(b"simulated trace")
    (experiment / "gazebo_imu.csv").write_text(
        "timestamp_s,x_g,y_g,z_g\n0.000000000,0,0,0\n0.076923077,0,0,0\n",
        encoding="utf-8",
    )
    elf = tmp_path / "zephyr.elf"
    elf.write_bytes(b"test elf placeholder")
    monkeypatch.setenv("RIOSE_ZEPHYR_ELF", str(elf))
    monkeypatch.setattr("riose.products.ear_tag.mvp3.bridge.shutil.which", lambda _: "/bin/renode-test")
    output = "\n".join((
        "Finished test 'gazebo-bridge.Firmware Reads Gazebo RESD Samples' in 1.0 seconds with status OK",
        "Finished test 'gazebo-bridge.Firmware Startup TX Payload Is Captured' in 1.0 seconds with status OK",
        "Finished test 'gazebo-bridge.LIS2DW12 Accepts Individual Gazebo Acceleration Samples' in 1.0 seconds with status OK",
        "movement WU evidence: tx=0x00000002 completed=0x00000002 wake_events=0x00000001 source_reads=0x00000001 samples_read=0x00000003 behavior=3 imu_mg=(575,793,0) WU_SRC=12 WU_IRQ=False CRC=True final_state=SLEEP",
        "Finished test 'gazebo-bridge.Firmware Wakes From Gazebo Motion Comparator And Transmits' in 1.0 seconds with status OK",
    ))

    def fake_run(*args, **kwargs):
        trace_path = kwargs["env"]["RIOSE_RENODE_TX_TRACE"]
        with open(trace_path, "w", encoding="utf-8") as stream:
            json.dump(_sx1262_trace(), stream)
        return SimpleNamespace(returncode=0, stdout=output)

    monkeypatch.setattr("riose.products.ear_tag.mvp3.bridge.subprocess.run",
                        fake_run)

    def fake_ingest(source, target):
        shutil.copy2(source, target / "firmware_trace.jsonl")
        return {"path": str(target / "firmware_trace.jsonl"),
                "source_path": str(source), "status": "SIMULATED",
                "power_status": "COMPLETED", "logical_rf_event_count": 1,
                "anchor_logical_accept_count": 1,
                "physical_rf_result": "ANTENNA_MODEL_UNVALIDATED", "rssi_dbm": None}

    monkeypatch.setattr("riose.products.ear_tag.mvp3.cli._ingest_firmware_trace",
                        fake_ingest)

    result = replay_in_renode(experiment)

    assert result["result"] == "PASS"
    assert result["tests"] == {"passed": 4, "failed": 0, "skipped": 0}
    assert result["movement_derived_wakeup"] == "VALIDATED"
    assert result["radio_transmission_from_movement"] == "GAZEBO_DERIVED_WU_COMPARATOR_PATH_VALIDATED"
    path = result["movement_wake_evidence"]
    assert path["post_boot_sample_read_count"] == 3
    assert path["post_boot_tx_count"] == 1
    assert path["pre_motion_firmware_state"] == "SLEEP"
    assert path["post_motion_firmware_state"] == "SLEEP"
    assert path["last_payload_axes_mg"] == [575, 793, 0]
    assert path["last_payload_crc_valid"] is True
    assert path["wake_up_source_register"] == "0x0C"
    assert path["wake_events_generated"] == 1
    assert path["wake_events_read_by_firmware"] == 1
    assert "approximation" in path["filter_model"]
    clock_mapping = result["clock_mapping"]
    assert clock_mapping["schema_version"] == "riose.mvp3.clock_mapping/v1"
    assert clock_mapping["status"] == "SIMULATED"
    assert clock_mapping["live_lockstep"] is False
    assert clock_mapping["alignment_check"]["status"] == "RECOMPUTED_FROM_RECORDS"
    assert clock_mapping["alignment_check"]["mapped_gazebo_time_s"] == 0.07715387
    assert clock_mapping["alignment_check"]["nearest_imu_sample_time_s"] == 0.076923077
    integration = result["automatic_firmware_trace_integration"]
    assert integration["status"] == "SIMULATED"
    assert (experiment / "sx1262_tx_trace.json").is_file()
    assert (experiment / "firmware_trace.jsonl").is_file()
    assert len((experiment / "firmware_trace.jsonl").read_text().splitlines()) == 7
    assert integration["integration"]["logical_rf_event_count"] == 1
    assert integration["integration"]["anchor_logical_accept_count"] == 1
    assert (experiment / "summary.json").is_file()
    manifest = json.loads((experiment / "manifest.json").read_text())
    assert manifest["automatic_firmware_trace_integration"] == "SIMULATED"
    assert manifest["anchor_receive_count"] == 1
    assert manifest["firmware_wake_tx_validated"] is True
    assert manifest["movement_wake_evidence"]["post_motion_firmware_state"] == "SLEEP"
    assert manifest["clock_mapping"]["schema_version"] == "riose.mvp3.clock_mapping/v1"
    assert "movement-derived wake/TX" in manifest["firmware_trace"]["source_scope"]


def test_renode_replay_fails_when_movement_wakeup_evidence_is_missing(tmp_path, monkeypatch) -> None:
    experiment = tmp_path / "walking"
    experiment.mkdir()
    (experiment / "gazebo_imu.resd").write_bytes(b"simulated trace")
    elf = tmp_path / "zephyr.elf"
    elf.write_bytes(b"test elf placeholder")
    monkeypatch.setenv("RIOSE_ZEPHYR_ELF", str(elf))
    monkeypatch.setattr("riose.products.ear_tag.mvp3.bridge.shutil.which", lambda _: "/bin/renode-test")
    output = "\n".join((
        "Finished test 'gazebo-bridge.Firmware Reads Gazebo RESD Samples' in 1.0 seconds with status OK",
        "Finished test 'gazebo-bridge.Firmware Startup TX Payload Is Captured' in 1.0 seconds with status OK",
        "Finished test 'gazebo-bridge.LIS2DW12 Accepts Individual Gazebo Acceleration Samples' in 1.0 seconds with status OK",
        "Finished test 'gazebo-bridge.Firmware Wakes From Gazebo Motion Comparator And Transmits' in 1.0 seconds with status OK",
    ))
    monkeypatch.setattr("riose.products.ear_tag.mvp3.bridge.subprocess.run",
                        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=output))

    try:
        replay_in_renode(experiment)
    except RuntimeError as exc:
        assert "did not pass" in str(exc)
    else:
        raise AssertionError("missing movement wake evidence must not be reported as a pass")

    summary = json.loads((experiment / "firmware_replay.json").read_text())
    assert summary["result"] == "FAILED"


def test_renode_replay_preserves_existing_canonical_trace_and_outputs(tmp_path, monkeypatch) -> None:
    experiment = tmp_path / "walking"
    experiment.mkdir()
    (experiment / "gazebo_imu.resd").write_bytes(b"simulated trace")
    (experiment / "firmware_trace.jsonl").write_text("caller trace\n", encoding="utf-8")
    (experiment / "rf_events.jsonl").write_text("existing rf outputs\n", encoding="utf-8")
    elf = tmp_path / "zephyr.elf"
    elf.write_bytes(b"test elf placeholder")
    monkeypatch.setenv("RIOSE_ZEPHYR_ELF", str(elf))
    monkeypatch.setattr("riose.products.ear_tag.mvp3.bridge.shutil.which", lambda _: "/bin/renode-test")
    output = "\n".join((
        "Finished test 'gazebo-bridge.Firmware Reads Gazebo RESD Samples' in 1.0 seconds with status OK",
        "Finished test 'gazebo-bridge.Firmware Startup TX Payload Is Captured' in 1.0 seconds with status OK",
        "Finished test 'gazebo-bridge.LIS2DW12 Accepts Individual Gazebo Acceleration Samples' in 1.0 seconds with status OK",
        "movement WU evidence: tx=0x00000002 completed=0x00000002 wake_events=0x00000001 source_reads=0x00000001 samples_read=0x00000003 behavior=3 imu_mg=(575,793,0) WU_SRC=12 WU_IRQ=False CRC=True final_state=SLEEP",
        "Finished test 'gazebo-bridge.Firmware Wakes From Gazebo Motion Comparator And Transmits' in 1.0 seconds with status OK",
    ))

    def fake_run(*args, **kwargs):
        with open(kwargs["env"]["RIOSE_RENODE_TX_TRACE"], "w", encoding="utf-8") as stream:
            json.dump(_sx1262_trace(), stream)
        return SimpleNamespace(returncode=0, stdout=output)

    monkeypatch.setattr("riose.products.ear_tag.mvp3.bridge.subprocess.run", fake_run)
    monkeypatch.setattr("riose.products.ear_tag.mvp3.cli._ingest_firmware_trace",
                        lambda *args: (_ for _ in ()).throw(AssertionError("must preserve existing trace")))

    result = replay_in_renode(experiment)

    assert result["automatic_firmware_trace_integration"]["status"] == "SKIPPED_EXISTING_FIRMWARE_TRACE"
    assert (experiment / "firmware_trace.jsonl").read_text(encoding="utf-8") == "caller trace\n"
    assert (experiment / "rf_events.jsonl").read_text(encoding="utf-8") == "existing rf outputs\n"
