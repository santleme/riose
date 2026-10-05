import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

BASE = Path(__file__).resolve().parents[1]

def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

adapter = load_module("trace_adapter", BASE / "trace_adapter.py")
power = load_module("mvp2_power", BASE / "mvp2_power.py")


def record(timestamp_ms, event, state, value0=0, sequence=None):
    return {
        "schema_version": "riose.firmware.trace/v1",
        "status": "SIMULATED", "timestamp_us": timestamp_ms * 1000,
        "state": state, "state_id": 0, "event": event, "event_id": 0,
        "source": "FIRMWARE", "source_id": 1, "result": 0,
        "value0": value0, "value1": 0, "value2": 0,
    } | ({"sequence": sequence} if sequence is not None else {})


class FirmwareTraceAdapterTests(unittest.TestCase):
    def test_orchestrator_profile_envelope_is_accepted(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "loads.json"
            path.write_text(json.dumps({
                "schema_version": "riose.power.loads/v1", "status": "ASSUMED",
                "loads": {"pair:TX_START:TX_DONE": {"current_status": "ASSUMED"}},
            }))
            loaded = adapter.read_load_profile(path)
        self.assertIn("pair:TX_START:TX_DONE", loaded)

    def test_schedule_is_accepted_by_power_analyzer(self):
        trace = [
            record(0, "STATE", "SLEEP"),
            record(1, "STATE", "IMU_MONITORING"),
            record(2, "IMU_READ", "IMU_MONITORING"),
            record(3, "STATE", "RF_TX"),
            record(3, "TX_START", "RF_TX"),
            record(7, "TX_DONE", "RF_TX"),
            record(7, "STATE", "RF_RX"),
            record(7, "RX_START", "RF_RX"),
            record(9, "RX_DONE", "RF_RX"),
            record(9, "STATE", "SLEEP"),
            record(9, "MCU_SLEEP", "SLEEP", value0=100),
        ]
        loads = {
            "state:SLEEP": {"component": "mcu_sleep", "load_current_ma": 0.01,
                             "current_status": "ASSUMED", "source": "test fixture"},
            "state:IMU_MONITORING": {"component": "mcu", "load_current_ma": 1,
                                      "current_status": "ASSUMED", "source": "test fixture"},
            "state:RF_TX": {"component": "mcu", "load_current_ma": 1,
                             "current_status": "ASSUMED", "source": "test fixture"},
            "state:RF_RX": {"component": "mcu", "load_current_ma": 1,
                             "current_status": "ASSUMED", "source": "test fixture"},
            "pair:TX_START:TX_DONE": {"component": "sx1262_tx", "load_current_ma": 20,
                                      "current_status": "ASSUMED", "source": "test fixture"},
            "pair:RX_START:RX_DONE": {"component": "sx1262_rx", "load_current_ma": 5,
                                      "current_status": "ASSUMED", "source": "test fixture"},
            "event:IMU_READ": {"component": "lis2dw12", "load_current_ma": 0.1,
                               "current_status": "ASSUMED", "source": "test fixture",
                               "fallback_duration_s": 0.001, "duration_status": "ASSUMED",
                               "duration_source": "test fixture"},
        }
        rows = adapter.trace_to_schedule(trace, loads)
        tx = next(row for row in rows if row["event"] == "TX")
        self.assertAlmostEqual(tx["duration_s"], 0.004)
        self.assertEqual(tx["duration_source"], "TRACE_TIMESTAMP")
        imu = next(row for row in rows if row["event"] == "IMU_READ")
        self.assertEqual(imu["duration_source"], "ASSUMED_FALLBACK")
        self.assertTrue(all(row["status"] == "SIMULATED" and
                            row["current_status"] == "ASSUMED" for row in rows))
        coverage = rows[0]["trace_event_coverage"]
        self.assertEqual(coverage["TX_START"]["handling"], "PAIRED_RADIO_INTERVAL")
        self.assertEqual(coverage["TX_DONE"]["count"], 1)
        self.assertEqual(coverage["STATE"]["handling"], "STRUCTURAL_MARKER_NO_SEPARATE_LOAD")

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "schedule.jsonl"
            path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
            accepted = power.load_schedule(path)
        result = power.analyze_schedule(accepted, {"idle_current_ma": {"value": 0.01}})
        self.assertEqual(result["status"], "SIMULATED")
        self.assertGreater(result["total_charge_mah_window"], 0)

    def test_firmware_control_markers_have_explicit_coverage_without_extra_loads(self):
        marker_names = ("SPI", "IRQ", "WAKE", "TIMEOUT", "TRACE_END")
        trace = [record(index, name, "SELF_TEST", sequence=index)
                 for index, name in enumerate(marker_names)]
        trace.append(record(5, "STATE", "SLEEP", sequence=5))
        trace.append(record(6, "MCU_SLEEP", "SLEEP", value0=10, sequence=6))
        loads = {"state:SELF_TEST": {
            "component": "mcu", "load_current_ma": 1,
            "current_status": "ASSUMED", "source": "test fixture",
            "fallback_duration_s": 0.01, "duration_status": "ASSUMED",
            "duration_source": "test fixture",
        }, "state:SLEEP": {
            "component": "mcu_sleep", "load_current_ma": 0.01,
            "current_status": "ASSUMED", "source": "test fixture",
        }}
        rows = adapter.trace_to_schedule(trace, loads)
        coverage = rows[0]["trace_event_coverage"]
        for name in marker_names:
            self.assertEqual(coverage[name]["handling"], "STRUCTURAL_MARKER_NO_SEPARATE_LOAD")
        self.assertFalse(any(row["event"] in marker_names for row in rows))

    def test_zero_resolution_requires_explicit_assumed_fallback(self):
        trace = [record(10, "TX_START", "RF_TX"), record(10, "TX_DONE", "RF_TX")]
        loads = {"pair:TX_START:TX_DONE": {
            "component": "radio", "load_current_ma": 10, "current_status": "ASSUMED",
            "source": "test fixture"
        }}
        with self.assertRaisesRegex(adapter.TraceConversionError, "zero timestamp resolution"):
            adapter.trace_to_schedule(trace, loads)

    def test_unpaired_radio_event_is_not_fabricated(self):
        trace = [record(10, "TX_START", "RF_TX")]
        loads = {"pair:TX_START:TX_DONE": {
            "component": "radio", "load_current_ma": 10, "current_status": "ASSUMED",
            "fallback_duration_s": 0.1, "duration_status": "ASSUMED",
            "source": "test fixture", "duration_source": "test fixture"
        }}
        with self.assertRaisesRegex(adapter.TraceConversionError, "cannot infer interval"):
            adapter.trace_to_schedule(trace, loads)

    def test_measured_current_is_rejected(self):
        trace = [record(0, "STATE", "SLEEP"),
                 record(1, "MCU_SLEEP", "SLEEP", value0=10)]
        loads = {"state:SLEEP": {
            "component": "mcu", "load_current_ma": 0.01,
            "current_status": "MEASURED", "source": "should not be accepted"
        }}
        with self.assertRaisesRegex(adapter.TraceConversionError, "must be ASSUMED"):
            adapter.trace_to_schedule(trace, loads)

    def test_blank_load_provenance_or_component_is_rejected(self):
        trace = [record(0, "STATE", "SLEEP"), record(1, "MCU_SLEEP", "SLEEP", value0=10)]
        base = {"component": "mcu", "load_current_ma": 0.01,
                "current_status": "ASSUMED", "source": "test fixture"}
        for key, value in (("source", "   "), ("component", "")):
            with self.subTest(key=key), self.assertRaises(adapter.TraceConversionError):
                adapter.trace_to_schedule(trace, {"state:SLEEP": {**base, key: value}})

    def test_versioned_trace_requires_contiguous_sequence(self):
        records = [record(0, "STATE", "SLEEP", sequence=0),
                   record(1, "STATE", "RF_TX", sequence=3)]
        with self.assertRaisesRegex(adapter.TraceConversionError, "contiguous"):
            adapter.trace_to_schedule(records, {})

    def test_radio_pairs_reject_orphan_nested_and_reordered_markers(self):
        config = {"pair:TX_START:TX_DONE": {
            "component": "radio", "load_current_ma": 10, "current_status": "ASSUMED",
            "source": "test fixture", "fallback_duration_s": 0.001,
            "duration_status": "ASSUMED", "duration_source": "test fixture"}}
        cases = [
            [record(0, "TX_DONE", "RF_TX"), record(1, "TX_START", "RF_TX")],
            [record(0, "TX_START", "RF_TX"), record(1, "TX_START", "RF_TX"),
             record(2, "TX_DONE", "RF_TX"), record(3, "TX_DONE", "RF_TX")],
            [record(0, "TX_START", "RF_TX"), record(1, "TX_DONE", "RF_TX"),
             record(2, "TX_DONE", "RF_TX")],
        ]
        for trace in cases:
            with self.subTest(trace=[row["event"] for row in trace]):
                with self.assertRaises(adapter.TraceConversionError):
                    adapter.trace_to_schedule(trace, config)

    def test_crossed_radio_types_and_boolean_timestamps_are_rejected(self):
        crossed = [record(0, "TX_START", "RF_TX"), record(1, "RX_START", "RF_RX"),
                   record(2, "TX_DONE", "RF_TX"), record(3, "RX_DONE", "RF_RX")]
        with self.assertRaisesRegex(adapter.TraceConversionError, "radio intervals overlap"):
            adapter.trace_to_schedule(crossed, {})
        with self.assertRaisesRegex(adapter.TraceConversionError, "not boolean"):
            adapter.trace_to_schedule([{**record(0, "STATE", "SLEEP"), "timestamp_us": True}], {})

    def test_trace_relative_window_includes_terminal_sleep_payload(self):
        trace = [record(100, "STATE", "SLEEP", sequence=0),
                 record(110, "STATE", "RF_TX", sequence=1),
                 record(120, "TX_START", "RF_TX", sequence=2),
                 record(130, "TX_DONE", "RF_TX", sequence=3),
                 record(130, "STATE", "SLEEP", sequence=4),
                 record(130, "MCU_SLEEP", "SLEEP", value0=1000, sequence=5)]
        loads = {
            "state:SLEEP": {"component": "mcu", "load_current_ma": 0.01,
                             "current_status": "ASSUMED", "source": "test fixture"},
            "state:RF_TX": {"component": "mcu", "load_current_ma": 1,
                             "current_status": "ASSUMED", "source": "test fixture"},
            "pair:TX_START:TX_DONE": {"component": "radio", "load_current_ma": 10,
                                      "current_status": "ASSUMED", "source": "test fixture"},
        }
        rows = adapter.trace_to_schedule(trace, loads)
        self.assertTrue(all(row["trace_window_start_s"] == 0 for row in rows))
        self.assertTrue(all(row["trace_window_end_s"] == 1.03 for row in rows))
        tx = next(row for row in rows if row["event"] == "TX")
        self.assertAlmostEqual(tx["timestamp_s"], 0.02)
        self.assertAlmostEqual(tx["duration_s"], 0.01)
        extended = adapter.trace_to_schedule(trace, loads, trace_duration_s=2.0)
        self.assertTrue(all(row["trace_window_end_s"] == 2.0 for row in extended))
        with self.assertRaisesRegex(adapter.TraceConversionError, "must be positive and cover"):
            adapter.trace_to_schedule(trace, loads, trace_duration_s=1.0)

    def test_unmodeled_trace_state_is_not_silently_dropped(self):
        trace = [record(0, "STATE", "BOOT"), record(1, "STATE", "SLEEP")]
        loads = {"state:SLEEP": {"component": "mcu", "load_current_ma": 0.01,
                                 "current_status": "ASSUMED", "source": "test fixture"}}
        with self.assertRaisesRegex(adapter.TraceConversionError, "state:BOOT.*required"):
            adapter.trace_to_schedule(trace, loads)

    def test_terminal_state_without_dwell_does_not_use_profile_fallback(self):
        trace = [record(0, "STATE", "BOOT"), record(10, "STATE", "SLEEP")]
        loads = {
            "state:BOOT": {"component": "mcu", "load_current_ma": 1,
                           "current_status": "ASSUMED", "source": "test fixture"},
            "state:SLEEP": {"component": "mcu", "load_current_ma": 0.01,
                             "fallback_duration_s": 900, "duration_status": "ASSUMED",
                             "current_status": "ASSUMED", "source": "test fixture"},
        }
        rows = adapter.trace_to_schedule(trace, loads)
        self.assertEqual(len(rows), 1)
        self.assertAlmostEqual(rows[0]["duration_s"], 0.01)
        self.assertAlmostEqual(rows[0]["trace_window_end_s"], 0.01)

    def test_unprofiled_events_and_double_counted_radio_markers_are_rejected(self):
        trace = [record(0, "STATE", "SLEEP"), record(1, "UNEXPECTED", "SLEEP")]
        loads = {"state:SLEEP": {"component": "mcu", "load_current_ma": 0.01,
                                  "current_status": "ASSUMED", "source": "test"}}
        with self.assertRaisesRegex(adapter.TraceConversionError, "UNEXPECTED.*no state, pair, point-load"):
            adapter.trace_to_schedule(trace, loads)

        radio_trace = [record(0, "TX_START", "RF_TX"), record(1, "TX_DONE", "RF_TX")]
        radio_loads = {
            "pair:TX_START:TX_DONE": {"component": "radio", "load_current_ma": 20,
                                      "current_status": "ASSUMED", "source": "test"},
            "event:TX_START": {"component": "radio", "load_current_ma": 20,
                               "current_status": "ASSUMED", "source": "test",
                               "fallback_duration_s": 0.001, "duration_status": "ASSUMED",
                               "duration_source": "test"},
        }
        with self.assertRaisesRegex(adapter.TraceConversionError, "cannot also be point loads"):
            adapter.trace_to_schedule(radio_trace, radio_loads)

    def test_hardware_bus_and_trace_lifecycle_markers_are_structural(self):
        trace = [record(0, "STATE", "SLEEP"), record(0, "SPI", "SLEEP"),
                 record(1, "IRQ", "SLEEP"), record(1, "TIMEOUT", "SLEEP"),
                 record(2, "WAKE", "SLEEP"), record(2, "TRACE_END", "SLEEP"),
                 record(3, "MCU_SLEEP", "SLEEP", value0=100)]
        loads = {"state:SLEEP": {"component": "mcu", "load_current_ma": 0.01,
                                 "current_status": "ASSUMED", "source": "test fixture"}}
        rows = adapter.trace_to_schedule(trace, loads)
        coverage = rows[0]["trace_event_coverage"]
        for event in ("SPI", "IRQ", "TIMEOUT", "WAKE", "TRACE_END"):
            self.assertEqual(coverage[event]["handling"], "STRUCTURAL_MARKER_NO_SEPARATE_LOAD")

    def test_point_event_interval_cannot_extend_beyond_trace_window(self):
        trace = [record(0, "IMU_READ", "IMU_MONITORING")]
        loads = {"event:IMU_READ": {
            "component": "imu", "load_current_ma": 1, "current_status": "ASSUMED",
            "source": "test", "fallback_duration_s": 1, "duration_status": "ASSUMED",
            "duration_source": "test",
        }}
        with self.assertRaisesRegex(adapter.TraceConversionError, "extends beyond the trace window"):
            adapter.trace_to_schedule(trace, loads)

    def test_cli_attaches_trace_hash_and_clears_old_schedule_on_invalid_input(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            trace_path, loads_path, output = root / "trace.jsonl", root / "loads.json", root / "schedule.jsonl"
            trace_path.write_text(json.dumps(record(0, "STATE", "SLEEP")) + "\n" +
                                  json.dumps(record(1, "STATE", "SLEEP")) + "\n")
            loads_path.write_text(json.dumps({"state:SLEEP": {
                "component": "mcu", "load_current_ma": 0.01,
                "current_status": "ASSUMED", "source": "fixture spec",
            }}))
            argv = ["trace_adapter", str(trace_path), "--loads", str(loads_path), "--output", str(output)]
            with patch.object(sys, "argv", argv):
                self.assertEqual(adapter.main(), 0)
            rows = [json.loads(line) for line in output.read_text().splitlines()]
            self.assertEqual(rows[0]["trace_provenance"]["status"], "SIMULATED")
            self.assertEqual(rows[0]["trace_provenance"]["sha256"],
                             __import__("hashlib").sha256(trace_path.read_bytes()).hexdigest())

            trace_path.write_text("{invalid json\n")
            with patch.object(sys, "argv", argv), patch.object(adapter.argparse.ArgumentParser, "error",
                    side_effect=SystemExit(2)):
                with self.assertRaises(SystemExit):
                    adapter.main()
            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
