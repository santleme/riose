"""Digital-twin report and stage-metric serialization."""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

def _metrics_csv(path: Path, stages: dict[str, dict[str, Any]]) -> None:
    rows = []
    for stage_name, result in stages.items():
        rows.append({"metric": f"stage.{stage_name}", "value": result.get("status"), "unit": "status",
                     "provenance": result.get("result_class", "ENVIRONMENT_OR_TEST_RESULT"),
                     "notes": result.get("detail", "")})
        for check in result.get("checks", []):
            rows.append({"metric": f"{stage_name}.{check.get('name', 'check')}",
                         "value": check.get("value", check.get("passed")),
                         "unit": check.get("unit", "boolean"), "provenance": check.get("status", "SIMULATED"),
                         "notes": check.get("source", "")})
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=["metric", "value", "unit", "provenance", "notes"], lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)

def _report(spec: dict[str, Any], summary: dict[str, Any]) -> str:
    gate = summary["gate"]
    stages = summary["stages"]
    power = stages.get("power", {})
    charge = power.get("modeled_charge_uah")
    event_charge = power.get("event_charge", [])
    largest_event = max(event_charge, key=lambda row: row.get("charge_uah", 0), default=None)
    long_runs = stages.get("long_duration_1_7_30_days", {})
    fault_stage = stages.get("adversarial_fault_injection", {})
    mechanical = stages.get("mechanical", {})
    antenna = stages.get("antenna", {})
    antenna_rows = antenna.get("scenarios", [])
    antenna_completed = sum(row.get("status") == "COMPLETED" for row in antenna_rows)
    failed_checks = mechanical.get("fit", {}).get("issues", [])
    if antenna_rows:
        antenna_answer = (f"{antenna_completed}/{len(antenna_rows)} cenários passaram a comparação numérica de malha; "
                          "resultados simulados não validam desempenho físico.")
    else:
        antenna_answer = antenna.get("detail", "Nenhum cenário de RF produziu resultado do solver.")
    energy_answer = (f"`{charge:.6g} µAh` pela integração SIMULATED de correntes ASSUMED/trace; "
                    f"ngspice: {power.get('ngspice_status', 'NOT_RUN')}." if charge is not None else
                    "Sem integração disponível; conferir o estágio power e seus bloqueadores.")
    largest_answer = (f"`{largest_event['event']}` / `{largest_event['component']}` "
                      f"({largest_event['charge_uah']:.6g} µAh) na janela simulada."
                      if largest_event else "Sem breakdown disponível.")
    lines = [
        "# RIOSE MVP 2 — Digital twin report", "",
        f"**Gate: {gate['state']}**", "",
        "Este relatório descreve um fluxo digital e SIMULATED. Nenhum hardware físico, laboratório ou medição foi usado. READY significaria apenas plausibilidade digital para justificar a fabricação do primeiro protótipo.", "",
        "## Execução", "",
        f"- Spec SHA-256: `{summary['spec_sha256']}`",
        f"- Plataforma: `{summary['environment']['platform']}`",
        f"- Parâmetros por status: `{json.dumps(summary['parameter_statuses'], sort_keys=True)}`",
        f"- GPU: `{json.dumps(summary['environment']['gpu'], sort_keys=True)}`", "",
        "## Estágios", "",
        "| Estágio | Status | Evidência/limitação |", "|---|---|---|",
    ]
    for name, result in stages.items():
        detail = str(result.get("detail", result.get("notes", ""))).replace("|", "\\|").replace("\n", " ")
        lines.append(f"| {name} | {result.get('status', 'UNKNOWN')} | {detail} |")
    lines += ["", "## Gate e bloqueadores", ""]
    if gate["blockers"]:
        lines += [f"- {item}" for item in gate["blockers"]]
    else:
        lines.append("Nenhum bloqueador digital registrado.")
    lines += ["", "## Respostas técnicas", "",
              f"1. Estabilidade do firmware: long runs C host `{long_runs.get('status', 'NOT_RUN')}`; Zephyr `{stages.get('zephyr_firmware', {}).get('status', 'NOT_RUN')}`.",
              f"2. Coerência dos periféricos virtuais: Renode `{stages.get('renode_firmware', {}).get('status', 'NOT_RUN')}`; {stages.get('renode_firmware', {}).get('detail', 'firmware/backend unavailable')}.",
              f"3. Energia digital estimada na janela observada: {energy_answer}",
              f"4. Estabilidade do rail: ngspice `{power.get('ngspice_status', 'NOT_RUN')}`; sem medição física ou resultado de rail quando não executado.",
              f"5. Evento com maior carga integrada: {largest_answer}",
              f"6. O envelope mecânico estimado cabe: análise de caixas delimitadoras `{mechanical.get('status', 'NOT_RUN')}`; {('; '.join(failed_checks) if failed_checks else 'sem conflito de envelope reportado')}.",
              f"7. Frequência de ressonância/S11: openEMS `{antenna.get('status', 'NOT_RUN')}`; {antenna_answer}",
              f"8. Degradação por PCB/bateria/carcaça/animal: {len(antenna_rows)} cenários listados; resultados exigem openEMS; aproximação animal é experimental.",
              f"9. Encaixe geométrico estimado: `{'PASS' if mechanical.get('fit', {}).get('fits') else 'BLOCKED'}`; CadQuery disponível `{mechanical.get('cadquery_available', False)}`. O resultado não valida montagem física.",
              f"10. Falhas encontradas: {summary.get('failure_count', 'ver failures.csv')} entradas; falhas de host cobertas `{', '.join(fault_stage.get('completed_host_cases', []))}`; pendentes `{', '.join(fault_stage.get('pending_cases', []))}`.",
              "11. Hipóteses a revisar: parâmetros ASSUMED e limites provisórios em hardware/spec.yaml; dimensões, antena e encaixe aguardam aprovação.",
              f"12. Parâmetros por status: `{json.dumps(summary['parameter_statuses'], sort_keys=True)}`; provenance completa na spec.",
              "13. Sem hardware real não são validados consumo, brownout, potência RF, sintonia, materiais ou comportamento animal.",
              "14. Este gate não é validação comercial, clínica ou de campo.", "",
              "## Integridade da evidência", "",
              "Nenhum campo MEASURED é permitido na spec do MVP2. Capacidades GPU são metadados de ambiente e o experimento Sionna é opcional.", ""]
    return "\n".join(lines)
