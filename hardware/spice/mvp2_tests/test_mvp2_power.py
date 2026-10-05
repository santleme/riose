import contextlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


MODULE = Path(__file__).resolve().parents[1] / "mvp2_power.py"
SPEC = importlib.util.spec_from_file_location("mvp2_power", MODULE)
power = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(power)


class TraceDrivenPowerTests(unittest.TestCase):
    def setUp(self):
        self.assumptions = json.loads((MODULE.parent / "mvp2_power_assumptions.json").read_text())
        self.rows = [
            {"timestamp_s": 0.0, "event": "WAKE", "state": "ACTIVE", "component": "mcu",
             "duration_s": 2.0, "load_current_ma": 10.0, "end_s": 2.0},
            {"timestamp_s": 1.0, "event": "TX_START", "state": "TX", "component": "radio",
             "duration_s": 1.0, "load_current_ma": 40.0, "end_s": 2.0},
        ]

    def test_ngspice_time_axis_allows_duplicate_discontinuity_samples(self):
        self.assertTrue(power._has_valid_time_axis([
            (0.0, 3.3, 3.6, 0.0),
            (1.0, 3.3, 3.6, 0.0),
            (1.0, 3.2, 3.5, 0.1),
            (2.0, 3.3, 3.6, 0.0),
        ]))
        self.assertFalse(power._has_valid_time_axis([(0.0,), (0.0,)]))
        self.assertFalse(power._has_valid_time_axis([(0.0,), (1.0,), (0.5,)]))

    def test_overlapping_event_energy_and_idle_gap_are_integrated(self):
        result = power.analyze_schedule(self.rows, self.assumptions, period_s=10.0,
            period_source="test repeat period", period_status="ASSUMED")
        # mA*s / 3600: (10*2 + 40*1 + idle*10) / 3600 mAh
        expected = (60 + self.assumptions["idle_current_ma"]["value"] * 10) / 3600
        self.assertAlmostEqual(result["total_charge_mah_window"], expected)
        self.assertAlmostEqual(result["mAh_per_day"], expected * 8640)
        self.assertEqual(result["status"], "SIMULATED")
        tx = next(r for r in result["event_charge"] if r["event"] == "TX_START")
        self.assertAlmostEqual(tx["charge_uah"], 40 / 3600 * 1000)
        self.assertIsNone(result["ideal_capacity_division"])
        self.assertEqual(result["ideal_capacity_division_status"],
                         "NOT_AVAILABLE_NO_NOMINAL_CAPACITY")

    def test_nominal_capacity_division_is_theoretical_and_provenance_complete(self):
        assumptions = {**self.assumptions, "nominal_capacity_mah": {
            "value": 1100, "unit": "mAh", "status": "DATASHEET",
            "source": "hardware/spec.yaml nominal capacity; rated at 1 mA to 2.0 V"}}
        result = power.analyze_schedule(self.rows, assumptions, period_s=10.0,
            period_source="test repeat period", period_status="ASSUMED")
        details = result["ideal_capacity_division_details"]
        self.assertAlmostEqual(result["ideal_capacity_division"], 1100 / result["mAh_per_day"])
        self.assertEqual(details["status"], "THEORETICAL_IDEAL_CAPACITY_DIVISION")
        self.assertEqual(details["classification"], "THEORETICAL_IDEAL_NOMINAL_ONLY")
        self.assertEqual(details["nominal_capacity"], assumptions["nominal_capacity_mah"])
        self.assertEqual(details["simulated_consumption"], result["mAh_per_day_provenance"])
        self.assertEqual(details["formula"],
                         "nominal_capacity_mAh / simulated_consumption_mAh_per_day")
        self.assertIn("does not represent usable capacity", details["caveat"])
        self.assertIn("predicted product autonomy", details["caveat"])
        self.assertNotIn("battery_life", details)
        self.assertNotIn("expected_autonomy", details)
        self.assertNotIn("MEASURED", details["status"])

    def test_ideal_capacity_division_fails_closed_for_invalid_capacity(self):
        consumption = 1.25
        provenance = {"value": consumption, "unit": "mAh/day",
            "status": "SIMULATED_EXTRAPOLATION_FROM_DECLARED_REPEAT_PERIOD", "source": "test trace"}
        valid = {"value": 1100, "unit": "mAh", "status": "DATASHEET", "source": "test spec"}
        self.assertEqual(power._ideal_capacity_division({}, consumption, provenance)["status"],
                         "NOT_AVAILABLE_NO_NOMINAL_CAPACITY")
        for value in (0, -1, float("nan"), float("inf"), -float("inf")):
            with self.subTest(value=value):
                item = {**valid, "value": value}
                result = power._ideal_capacity_division({"nominal_capacity_mah": item}, consumption, provenance)
                self.assertIsNone(result["value_days"])
                self.assertEqual(result["status"], "NOT_AVAILABLE_INVALID_NOMINAL_CAPACITY")
        result = power._ideal_capacity_division(
            {"nominal_capacity_mah": {**valid, "unit": "Ah"}}, consumption, provenance)
        self.assertEqual(result["status"], "NOT_AVAILABLE_INVALID_NOMINAL_CAPACITY_UNIT")
        result = power._ideal_capacity_division(
            {"nominal_capacity_mah": {**valid, "source": "   "}}, consumption, provenance)
        self.assertEqual(result["status"], "NOT_AVAILABLE_MISSING_CAPACITY_PROVENANCE")

    def test_ideal_capacity_division_fails_closed_for_invalid_or_unproven_daily_consumption(self):
        capacity = {"nominal_capacity_mah": {"value": 1100, "unit": "mAh",
            "status": "DATASHEET", "source": "test spec"}}
        valid_provenance = {"value": 1.0, "unit": "mAh/day",
            "status": "SIMULATED_EXTRAPOLATION_FROM_DECLARED_REPEAT_PERIOD", "source": "test trace"}
        for consumption in (None, 0, -1, float("nan"), float("inf")):
            with self.subTest(consumption=consumption):
                result = power._ideal_capacity_division(capacity, consumption, valid_provenance)
                self.assertIsNone(result["value_days"])
                self.assertEqual(result["status"], "NOT_AVAILABLE_INVALID_SIMULATED_CONSUMPTION")
        for provenance in (None, {}, {**valid_provenance, "source": ""},
                           {**valid_provenance, "status": "MEASURED"},
                           {**valid_provenance, "unit": "Ah/day"}):
            with self.subTest(provenance=provenance):
                result = power._ideal_capacity_division(capacity, 1.0, provenance)
                self.assertIsNone(result["value_days"])
                self.assertEqual(result["status"], "NOT_AVAILABLE_MISSING_CONSUMPTION_PROVENANCE")
        valid_result = power._ideal_capacity_division(capacity, 1.0, valid_provenance)
        invalid_result = power._ideal_capacity_division(capacity, None, None)
        self.assertEqual(valid_result["value_days"], 1100)
        self.assertIsNone(invalid_result["value_days"])

    def test_daily_projection_requires_declared_repeat_period(self):
        result = power.analyze_schedule(self.rows, self.assumptions)
        self.assertIsNone(result["mAh_per_day"])
        self.assertEqual(result["mAh_per_day_status"], "NOT_REPORTED_NO_REPEAT_PERIOD")
        with self.assertRaisesRegex(ValueError, "source provenance"):
            power.analyze_schedule(self.rows, self.assumptions, period_s=10.0)

    def test_repeat_period_requires_finite_seconds_and_supported_provenance(self):
        good = {"period_source": "hardware/spec.yaml beacon interval", "period_status": "SIMULATED"}
        for period in (0.0, -1.0, float("nan"), float("inf")):
            with self.subTest(period=period), self.assertRaises(ValueError):
                power.analyze_schedule(self.rows, self.assumptions, period_s=period, **good)
        with self.assertRaisesRegex(ValueError, "period_unit"):
            power.analyze_schedule(self.rows, self.assumptions, period_s=10.0,
                                   period_source="test", period_status="ASSUMED", period_unit="ms")
        with self.assertRaisesRegex(ValueError, "status and source"):
            power.analyze_schedule(self.rows, self.assumptions, period_s=10.0,
                                   period_source="test", period_status="MEASURED")
        for invalid_source, invalid_status in (("   ", "ASSUMED"), ([], "ASSUMED"),
                                                ("test", [])):
            with self.subTest(source=invalid_source, status=invalid_status), self.assertRaises(ValueError):
                power.analyze_schedule(self.rows, self.assumptions, period_s=10.0,
                                       period_source=invalid_source, period_status=invalid_status)
        result = power.analyze_schedule(self.rows, self.assumptions, period_s=10.0, **good)
        self.assertEqual(result["repeat_period_provenance"], {
            "value": 10.0, "unit": "s", "status": "SIMULATED", "source": good["period_source"]})

    def test_netlist_uses_trace_load_and_assumption_provenance(self):
        deck = power.generate_netlist(self.rows, self.assumptions, period_s=10)
        self.assertIn("SIMULATED", deck)
        self.assertIn("ASSUMED", deck)
        self.assertIn("PWL(", deck)
        self.assertIn("meas tran rail_min", deck)
        self.assertIn("Ibattery battery 0 PWL(", deck)
        self.assertIn("tran 0.0002 2 0 0.0002", deck)
        self.assertIn("Breg reg_src 0", deck)
        self.assertIn("v(battery)-0.2", deck)

    def test_electrical_fault_profiles_are_connected_to_rail_and_assumed(self):
        for profile in ("voltage_drop", "high_esr", "regulator_instability"):
            with self.subTest(profile=profile):
                deck = power.generate_netlist(self.rows, self.assumptions, fault_profile=profile)
                self.assertIn(f"profile={profile}", deck)
                self.assertIn("Breg reg_src 0", deck)
                self.assertIn("Rcell cell_src battery", deck)
        voltage = power.generate_netlist(self.rows, self.assumptions, fault_profile="voltage_drop")
        self.assertIn("drop=1.2 V", voltage)
        high_esr = power.generate_netlist(self.rows, self.assumptions, fault_profile="high_esr")
        self.assertIn("RBAT=40", high_esr)
        unstable = power.generate_netlist(self.rows, self.assumptions, fault_profile="regulator_instability")
        self.assertIn("sin(2*pi*100*time)", unstable)
        with self.assertRaisesRegex(ValueError, "unsupported electrical fault"):
            power.generate_netlist(self.rows, self.assumptions, fault_profile="invented")

    def test_schedule_rejects_negative_or_zero_intervals(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "bad.jsonl"
            source.write_text(json.dumps({**self.rows[0], "timestamp_s": -1}) + "\n")
            with self.assertRaises(ValueError):
                power.load_schedule(source)
        with self.assertRaises(ValueError):
            power.analyze_schedule(self.rows, self.assumptions, period_s=1)

    def test_sweeps_are_one_factor_at_a_time_and_temperature_is_not_fabricated(self):
        cases = power.parameter_sweep(self.rows, self.assumptions, period_s=10)
        self.assertGreaterEqual(len(cases), 16)
        temperature = next(c for c in cases if c["overrides"].get("temperature_c_assumption") == -10.0)
        self.assertEqual(temperature["temperature_status"], "ASSUMED_SCENARIO_ONLY_NO_TEMPERATURE_MODEL")
        self.assertEqual(temperature["status"], "NOT_MODELED_NO_TEMPERATURE_DEPENDENCY")
        self.assertIsNone(temperature["netlist"])
        self.assertEqual(temperature["override_provenance"]["temperature_c_assumption"]["status"], "ASSUMED")

    def _with_temperature_model(self):
        assumptions = json.loads(json.dumps(self.assumptions))
        def record(value, unit):
            return {"value": value, "unit": unit, "status": "ASSUMED",
                    "source": "Synthetic unit-test coefficient; no component characterization"}
        assumptions["temperature_model"] = {
            "kind": "LINEAR_PARAMETER_SENSITIVITY",
            "reference_temperature_c": record(25, "degC"),
            "minimum_temperature_c": record(-10, "degC"),
            "maximum_temperature_c": record(50, "degC"),
            "coefficients": {"battery_esr_ohm": record(-0.002, "ohm/degC"),
                             "regulator_efficiency": record(0.001, "fraction/degC"),
                             "regulator_dropout_v": record(-0.001, "V/degC"),
                             "output_capacitance_f": record(1e-8, "F/degC")},
        }
        return assumptions

    def test_temperature_model_changes_electrical_parameters_and_preserves_provenance(self):
        assumptions = self._with_temperature_model()
        cases = power.parameter_sweep(self.rows, assumptions)
        temperatures = [c for c in cases if "temperature_c_assumption" in c["overrides"]]
        cold, reference, warm = temperatures
        self.assertEqual(len(temperatures), 3)
        resolved = cold["temperature_model"]["resolved_parameters"]
        self.assertAlmostEqual(resolved["battery_esr_ohm"], 0.32)
        self.assertAlmostEqual(resolved["regulator_efficiency"], 0.815)
        self.assertAlmostEqual(resolved["regulator_dropout_v"], 0.235)
        self.assertAlmostEqual(resolved["output_capacitance_f"], 0.00004665)
        self.assertIn("RBAT=0.32", cold["netlist"])
        self.assertIn("v(battery)-0.235", cold["netlist"])
        self.assertNotEqual(cold["netlist"], warm["netlist"])
        for key, value in reference["temperature_model"]["resolved_parameters"].items():
            self.assertEqual(value, assumptions[key]["value"])
        self.assertEqual(cold["status"], "PENDING_EXECUTION")
        self.assertEqual(cold["temperature_model"]["definition"], assumptions["temperature_model"])
        self.assertEqual(cold["temperature_model"]["reference_parameters"]["battery_esr_ohm"],
                         assumptions["battery_esr_ohm"])
        self.assertEqual(cold["temperature_model"]["status"],
                         "SIMULATED_PARAMETER_SENSITIVITY_NOT_CHARACTERIZED")
        self.assertEqual(cold["override_provenance"]["temperature_c_assumption"]["unit"], "degC")

    def test_temperature_model_requires_provenance_units_and_finite_coefficients(self):
        for field, value in (("unit", "ohm"), ("source", " "), ("status", "MEASURED"),
                             ("value", float("nan")), ("value", True), ("value", float("inf"))):
            assumptions = self._with_temperature_model()
            assumptions["temperature_model"]["coefficients"]["battery_esr_ohm"][field] = value
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                power.parameter_sweep(self.rows, assumptions)
        for field, value in (("unit", "K"), ("source", ""), ("status", "VALIDATED")):
            assumptions = self._with_temperature_model()
            assumptions["temperature_model"]["reference_temperature_c"][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                power.parameter_sweep(self.rows, assumptions)

    def test_temperature_model_rejects_extrapolation_and_nonphysical_parameters(self):
        for field, value in (("minimum_temperature_c", 0), ("maximum_temperature_c", 30),
                             ("minimum_temperature_c", -300), ("reference_temperature_c", 100)):
            assumptions = self._with_temperature_model()
            assumptions["temperature_model"][field]["value"] = value
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                power.parameter_sweep(self.rows, assumptions)
        for key, value in (("battery_esr_ohm", 1), ("regulator_efficiency", 0.1),
                           ("regulator_dropout_v", 1), ("output_capacitance_f", 1)):
            assumptions = self._with_temperature_model()
            assumptions["temperature_model"]["coefficients"][key]["value"] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                power.parameter_sweep(self.rows, assumptions)

    def test_temperature_model_rejects_missing_zero_and_unknown_sensitivities(self):
        for model in ({}, {"kind": "invented"}, {"kind": "LINEAR_PARAMETER_SENSITIVITY"}):
            with self.subTest(model=model), self.assertRaises(ValueError):
                power.parameter_sweep(self.rows, {**self.assumptions, "temperature_model": model})
        for coefficients in ({}, {"invented": {}}):
            assumptions = self._with_temperature_model()
            assumptions["temperature_model"]["coefficients"] = coefficients
            with self.subTest(coefficients=coefficients), self.assertRaises(ValueError):
                power.parameter_sweep(self.rows, assumptions)
        assumptions = self._with_temperature_model()
        for record in assumptions["temperature_model"]["coefficients"].values():
            record["value"] = 0
        with self.assertRaisesRegex(ValueError, "nonzero sensitivity"):
            power.parameter_sweep(self.rows, assumptions)

    def test_configured_temperature_sweep_executes_all_cases_and_propagates_failure(self):
        assumptions = self._with_temperature_model()
        def passing(deck, binary, rows):
            return {"status": "PASS", "rail_min_v": 3.2, "rail_max_v": 3.3}
        with tempfile.TemporaryDirectory() as tmp, patch.object(power, "_run_ngspice", side_effect=passing) as run:
            result = power.execute_parameter_sweep(self.rows, assumptions, Path(tmp))
        self.assertEqual(result["case_count"], 19)
        self.assertEqual(result["attempted_case_count"], 19)
        self.assertEqual(result["executed_case_count"], 19)
        self.assertEqual(result["not_modeled_axes"], [])
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(run.call_count, 19)
        temp = next(c for c in result["cases"] if "temperature_c_assumption" in c["overrides"])
        self.assertEqual(temp["temperature_status"], "SIMULATED_WITH_DECLARED_PARAMETER_MODEL")
        self.assertEqual(temp["temperature_model"]["definition"], assumptions["temperature_model"])
        self.assertNotIn("netlist", temp)
        def fail_cold(deck, binary, rows):
            if "temperature_c_assumption--10" in str(deck):
                return {"status": "NON_CONVERGED", "rail_min_v": None}
            return passing(deck, binary, rows)
        with tempfile.TemporaryDirectory() as tmp, patch.object(power, "_run_ngspice", side_effect=fail_cold):
            result = power.execute_parameter_sweep(self.rows, assumptions, Path(tmp))
        self.assertEqual(result["status"], "NON_CONVERGED")
        self.assertEqual(result["executed_case_count"], 18)

    def test_energy_metrics_use_assumed_rail_voltage_and_cover_event_components(self):
        result = power.analyze_schedule(self.rows, self.assumptions, period_s=10.0,
            period_source="test repeat period", period_status="ASSUMED")
        self.assertAlmostEqual(result["energy_tx_mj"], 40 / 3600 * 1000 * 3.3 * 3.6)
        self.assertEqual(result["energy_status"], "SIMULATED_AT_ASSUMED_REGULATOR_OUTPUT_VOLTAGE")
        self.assertGreater(result["energy_wake_mj"], 0)
        self.assertEqual(result["model_provenance"]["regulator_output_v"]["status"], "ASSUMED")

    def test_trace_metadata_extends_integrated_window_and_daily_projection(self):
        rows = [dict(self.rows[0], trace_window_start_s=0.0, trace_window_end_s=10.0),
                dict(self.rows[1], trace_window_start_s=0.0, trace_window_end_s=10.0)]
        result = power.analyze_schedule(rows, self.assumptions, period_s=20.0,
            period_source="test repeat period", period_status="ASSUMED")
        self.assertEqual(result["modeled_window_s"], 20.0)
        self.assertIsNotNone(result["mAh_per_day"])
        self.assertEqual(result["mAh_per_day_status"], "SIMULATED_EXTRAPOLATION_FROM_DECLARED_REPEAT_PERIOD")
        with self.assertRaisesRegex(ValueError, "disagree on trace window"):
            power.analyze_schedule([rows[0], dict(rows[1], trace_window_end_s=11.0)], self.assumptions)

    def test_invalid_electrical_overrides_are_rejected(self):
        for override in ({"regulator_efficiency": 0}, {"battery_voltage_v": float("nan")},
                         {"output_capacitance_f": -1}):
            with self.subTest(override=override):
                with self.assertRaises(ValueError):
                    power.generate_netlist(self.rows, self.assumptions, overrides=override)

    def test_ngspice_success_requires_measurements_and_valid_waveform(self):
        with tempfile.TemporaryDirectory() as tmp:
            deck = Path(tmp) / "power_trace.cir"
            deck.write_text(".param VREG=3.3\n.tran 0.1 2 0 0.1\n")
            completed = __import__("subprocess").CompletedProcess([], 0, "rail_min = 3.1\nrail_max = 3.3\nbattery_min = 3.5\nbattery_current_peak = -0.02\n", "")
            def run(*args, **kwargs):
                (Path(tmp) / "power_waveform.dat").write_text("0 3.3 3.6 -0.01\n1 3.1 3.5 -0.02\n2 3.3 3.6 -0.01\n")
                return completed
            with patch.object(power.shutil, "which", return_value="ngspice"), patch.object(power.subprocess, "run", side_effect=run):
                result = power._run_ngspice(deck, None, self.rows)
        self.assertEqual(result["status"], "PASS")
        self.assertAlmostEqual(result["voltage_droop_v"], 0.2)
        self.assertAlmostEqual(result["battery_current_peak_a"], 0.02)

    def test_ngspice_missing_convergence_results_is_failed_not_executed(self):
        with tempfile.TemporaryDirectory() as tmp:
            deck = Path(tmp) / "power_trace.cir"
            deck.write_text(".param VREG=3.3\n.tran 0.1 2 0 0.1\n")
            completed = __import__("subprocess").CompletedProcess([], 0, "tran analysis failed: timestep too small\n", "")
            with patch.object(power.shutil, "which", return_value="ngspice"), patch.object(power.subprocess, "run", return_value=completed):
                result = power._run_ngspice(deck, None, self.rows)
        self.assertEqual(result["status"], "NON_CONVERGED")
        self.assertIn("convergence/analysis failure", result["detail"])

    def test_ngspice_statuses_distinguish_unavailable_failure_and_invalid_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            deck = Path(tmp) / "power_trace.cir"
            deck.write_text(".param VREG=3.3\n.tran 0.1 2 0 0.1\n")
            with patch.object(power.shutil, "which", return_value=None):
                self.assertEqual(power._run_ngspice(deck, None, self.rows)["status"], "TOOL_UNAVAILABLE")
            completed = __import__("subprocess").CompletedProcess([], 9, "solver failed\n", "")
            with patch.object(power.shutil, "which", return_value="ngspice"), patch.object(
                    power.subprocess, "run", return_value=completed):
                self.assertEqual(power._run_ngspice(deck, None, self.rows)["status"], "SIMULATION_FAILED")

            completed = __import__("subprocess").CompletedProcess([], 0,
                "rail_min = 3.1\nrail_max = 3.3\nbattery_min = 3.5\nbattery_current_peak = -0.02\n", "")
            with patch.object(power.shutil, "which", return_value="ngspice"), patch.object(
                    power.subprocess, "run", side_effect=lambda *args, **kwargs: (
                        (Path(tmp) / "power_waveform.dat").write_text("0 3.3 3.6 -0.01\n"), completed)[1]):
                result = power._run_ngspice(deck, None, self.rows)
            self.assertEqual(result["status"], "INVALID_OUTPUT")
            self.assertIsNone(result["rail_min_v"])
            self.assertFalse((Path(tmp) / "electrical_trace.csv").exists())

    def test_waveform_truncation_malformed_rows_and_nan_are_invalid(self):
        measurement_log = "rail_min = 3.1\nrail_max = 3.3\nbattery_min = 3.5\nbattery_current_peak = -0.02\n"
        for waveform in (
            "0 3.3 3.6 -0.01\n1 3.1 3.5 -0.02\n",  # truncated before .tran stop=2
            "0 3.3 3.6 -0.01\nnot-a-sample\n2 3.3 3.6 -0.01\n",
            "0 3.3 3.6 -0.01\n1 nan 3.5 -0.02\n2 3.3 3.6 -0.01\n",
        ):
            with self.subTest(waveform=waveform), tempfile.TemporaryDirectory() as tmp:
                deck = Path(tmp) / "power_trace.cir"
                deck.write_text(".param VREG=3.3\n.tran 0.1 2 0 0.1\n")
                (Path(tmp) / "power_waveform.dat").write_text("stale old waveform\n")
                completed = __import__("subprocess").CompletedProcess([], 0, measurement_log, "")
                with patch.object(power.shutil, "which", return_value="ngspice"), patch.object(
                        power.subprocess, "run", side_effect=lambda *args, **kwargs: (
                            (Path(tmp) / "power_waveform.dat").write_text(waveform), completed)[1]):
                    result = power._run_ngspice(deck, None, self.rows)
                self.assertEqual(result["status"], "INVALID_OUTPUT")
                self.assertFalse((Path(tmp) / "electrical_trace.csv").exists())

    def test_stale_waveform_and_csv_are_removed_when_next_run_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            deck = root / "power_trace.cir"
            deck.write_text(".param VREG=3.3\n.tran 0.1 2 0 0.1\n")
            waveform = root / "power_waveform.dat"
            electrical = root / "electrical_trace.csv"
            completed = __import__("subprocess").CompletedProcess([], 0,
                "rail_min = 3.1\nrail_max = 3.3\nbattery_min = 3.5\nbattery_current_peak = -0.02\n", "")
            def succeeds(*args, **kwargs):
                waveform.write_text("0 3.3 3.6 -0.01\n1 3.1 3.5 -0.02\n2 3.3 3.6 -0.01\n")
                return completed
            with patch.object(power.shutil, "which", return_value="ngspice"), patch.object(
                    power.subprocess, "run", side_effect=succeeds):
                self.assertEqual(power._run_ngspice(deck, None, self.rows)["status"], "PASS")
            self.assertTrue(electrical.exists())
            failure = __import__("subprocess").CompletedProcess([], 1, "failed\n", "")
            with patch.object(power.shutil, "which", return_value="ngspice"), patch.object(
                    power.subprocess, "run", return_value=failure):
                result = power._run_ngspice(deck, None, self.rows)
            self.assertEqual(result["status"], "SIMULATION_FAILED")
            self.assertFalse(waveform.exists())
            self.assertFalse(electrical.exists())

    def test_invalid_numeric_and_unit_assumptions_fail_closed(self):
        for key, value in (("battery_voltage_v", float("nan")),
                           ("battery_esr_ohm", float("inf")),
                           ("idle_current_ma", float("nan"))):
            assumptions = json.loads(json.dumps(self.assumptions))
            assumptions[key]["value"] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                power.generate_netlist(self.rows, assumptions)
        assumptions = json.loads(json.dumps(self.assumptions))
        assumptions["battery_voltage_v"]["unit"] = "mV"
        with self.assertRaisesRegex(ValueError, "unit must be V"):
            power.generate_netlist(self.rows, assumptions)

    def test_metrics_keep_interval_provenance_and_dimensional_conversions(self):
        rows = [dict(self.rows[0], provenance="spec:MCU assumed", current_status="ASSUMED",
                     duration_source="trace timestamp")]
        result = power.analyze_schedule(rows, self.assumptions)
        active = next(r for r in result["event_charge"] if r["event"] == "WAKE")
        self.assertEqual(active["provenance"], ["spec:MCU assumed"])
        self.assertEqual(active["duration_provenance"], ["trace timestamp"])
        # 10 mA * 2 s = 5.555... uAh; at 3.3 V this is 0.066 mJ.
        self.assertAlmostEqual(active["charge_uah"], 10 * 2 / 3600 * 1000)
        self.assertAlmostEqual(active["energy_mj"], (10 * 2 / 3600 * 1000) * 3.3 * 3.6)

    def test_sweep_executes_each_electrical_case_and_marks_temperature_unmodeled(self):
        def passing(deck, binary, rows):
            (deck.parent / "electrical_trace.csv").write_text("timestamp_s,rail_voltage_v\n")
            return {"status": "PASS", "rail_min_v": 3.2, "rail_max_v": 3.3}
        with tempfile.TemporaryDirectory() as tmp, patch.object(power, "_run_ngspice", side_effect=passing) as run:
            result = power.execute_parameter_sweep(self.rows, self.assumptions, Path(tmp))
        self.assertEqual(result["case_count"], 19)
        self.assertEqual(result["attempted_case_count"], 16)
        self.assertEqual(result["executed_case_count"], 16)
        self.assertEqual(result["status"], "PASS_WITH_TEMPERATURE_AXIS_NOT_MODELED")
        self.assertEqual(run.call_count, 16)
        temp = next(case for case in result["cases"] if "temperature_c_assumption" in case["overrides"])
        self.assertEqual(temp["status"], "NOT_MODELED_NO_TEMPERATURE_DEPENDENCY")
        self.assertNotIn("ngspice", temp)

    def test_cli_rejects_invalid_temperature_model_before_any_simulation(self):
        assumptions = self._with_temperature_model()
        assumptions["temperature_model"]["minimum_temperature_c"]["value"] = 0
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "output"
            schedule = Path(tmp) / "schedule.jsonl"
            schedule.write_text("\n".join(json.dumps(row) for row in self.rows) + "\n")
            inputs = Path(tmp) / "assumptions.json"
            inputs.write_text(json.dumps(assumptions))
            with contextlib.redirect_stderr(io.StringIO()), patch.object(power, "_run_ngspice") as run:
                code = power.main([str(schedule), "--assumptions", str(inputs), "--output", str(output)])
            self.assertEqual(code, 2)
            run.assert_not_called()
            summary = json.loads((output / "summary.json").read_text())
            self.assertEqual(summary["status"], "INVALID_INPUT")
            self.assertFalse((output / "power_trace.cir").exists())

    def test_cli_temperature_cases_keep_provenance_when_ngspice_is_unavailable(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "output"
            schedule = Path(tmp) / "schedule.jsonl"
            schedule.write_text("\n".join(json.dumps(row) for row in self.rows) + "\n")
            inputs = Path(tmp) / "assumptions.json"
            assumptions = self._with_temperature_model()
            inputs.write_text(json.dumps(assumptions))
            with contextlib.redirect_stdout(io.StringIO()):
                code = power.main([str(schedule), "--assumptions", str(inputs), "--output", str(output),
                                   "--ngspice", str(Path(tmp) / "missing-ngspice")])
            self.assertEqual(code, 0)
            summary = json.loads((output / "summary.json").read_text())
            sweep = json.loads((output / "sweep.json").read_text())
            self.assertEqual(sweep["status"], "TOOL_UNAVAILABLE")
            self.assertEqual(sweep["attempted_case_count"], 19)
            self.assertEqual(sweep["executed_case_count"], 0)
            self.assertEqual(sweep["not_modeled_axes"], [])
            temperature = next(c for c in sweep["cases"] if "temperature_c_assumption" in c["overrides"])
            self.assertEqual(temperature["temperature_model"]["definition"], assumptions["temperature_model"])
            self.assertEqual(summary["sweep"], sweep)
            self.assertEqual(temperature["status"], "TOOL_UNAVAILABLE")

    def test_cli_requires_and_records_repeat_period_provenance(self):
        with tempfile.TemporaryDirectory() as tmp:
            schedule = Path(tmp) / "schedule.jsonl"
            schedule.write_text("\n".join(json.dumps(row) for row in self.rows) + "\n")
            with contextlib.redirect_stderr(io.StringIO()):
                code = power.main([str(schedule), "--period-s", "10", "--output", str(Path(tmp) / "missing")])
            self.assertEqual(code, 2)
            self.assertEqual(json.loads((Path(tmp) / "missing" / "summary.json").read_text())["status"],
                             "INVALID_INPUT")
            output = Path(tmp) / "output"
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(power.main([
                    str(schedule), "--period-s", "10", "--period-source", "test fixture",
                    "--period-status", "ASSUMED",
                    "--ngspice", str(Path(tmp) / "missing-ngspice"),
                    "--output", str(output)]), 0)
            summary = json.loads((output / "summary.json").read_text())
            self.assertEqual(summary["repeat_period_provenance"]["source"], "test fixture")
            self.assertEqual(summary["ngspice"]["status"], "TOOL_UNAVAILABLE")
            self.assertFalse((output / "electrical_trace.csv").exists())


if __name__ == "__main__":
    unittest.main()
