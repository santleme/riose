# RIOSE MVP 2 — Digital twin report

**Gate: NOT_READY_FOR_PHYSICAL_PROTOTYPE**

Este relatório descreve um fluxo digital e SIMULATED. Nenhum hardware físico, laboratório ou medição foi usado. READY significaria apenas plausibilidade digital para justificar a fabricação do primeiro protótipo.

## Execução

- Spec SHA-256: `d5af1cc24253f70e6a337551038096504267dc1311178b53ef1f585b6340aef8`
- Plataforma: `Linux-6.6.87.2-microsoft-standard-WSL2-x86_64-with-glibc2.39`
- Parâmetros por status: `{"ASSUMED": 78, "DATASHEET": 18, "SIMULATED": 3}`
- GPU: `{"CUDA_AVAILABLE": false, "GPU_AVAILABLE": false, "GPU_TYPE": "NONE_DETECTED", "SIONNA_AVAILABLE": false, "experiment": "OPTIONAL_GPU_EXPERIMENT", "result_status": "ENVIRONMENT_CAPABILITY_ONLY", "status": "SKIPPED_OPTIONAL"}`

## Estágios

| Estágio | Status | Evidência/limitação |
|---|---|---|
| adversarial_fault_injection | PARTIAL | Host C tests cover transient I2C/SPI/TX recovery, CRC rejection, bounded SX1262 BUSY wait, TX/RX IRQ deadlines, and deterministic reset-flag classification. The Zephyr target build configures the MCU watchdog, but reset behavior is not physically executed. Analog power faults require electrical models; unexpected reboot needs a persistent expected-reset contract |
| antenna | PARTIAL_OR_BLOCKED | ANTENNA_FREE_SPACE: FAILED (mesh refinement did not meet declared numerical convergence criteria); ANTENNA_WITH_PCB: FAILED (mesh refinement did not meet declared numerical convergence criteria); ANTENNA_WITH_BATTERY: FAILED (openEMS candidate simulation failed closed: RuntimeError: openEMS did not reach the -40 dB field-energy criterion before its timestep limit; log=/home/lucas_coimbra/.codex/worktrees/0208/riose/results/mvp2/antenna/ANTENNA_WITH_BATTERY/mesh_4mm/solver.log; tail: RunFDTD: Warning: Max. number of timesteps was reached before the end-criteria of -40dB was reached...  	You may want to choose a higher number of max. timesteps...  Time for 160000 iterations with 63168.00 cells : 180.04 sec Speed: 56.14 MCells/s ); ANTENNA_NEAR_ANIMAL_APPROXIMATION: FAILED (openEMS candidate simulation failed closed: RuntimeError: S11 minimum lies on the sweep boundary; resonance is not bracketed) |
| firmware_scenarios | COMPLETED | C FSM traces exported for all four requested profiles |
| four_power_scenarios | COMPLETED | NORMAL/ACTIVE/ALERT/WORST_REASONABLE_CASE were converted from firmware traces and analyzed |
| gpu_optional | SKIPPED_OPTIONAL | Sionna RT is optional; no GPU scenario blocks the digital twin core |
| host_trace_export | PASSED |  |
| long_duration_1_7_30_days | COMPLETED | 12 deterministic FSM long runs completed |
| mechanical | COMPLETED | Bounding-box fit estimate completed |
| mvp1_c_tests | PASSED |  |
| power | COMPLETED | Four scenario rail simulations completed |
| renode_firmware | PASSED | Renode platform smoke and Zephyr ELF execution completed |
| synthetic_motion | COMPLETED |  |
| trace_schedule | PASSED |  |
| zephyr_firmware | PASSED | Built target firmware for nucleo_l031k6 |

## Gate e bloqueadores

- adversarial_fault_injection: PARTIAL
- antenna: PARTIAL_OR_BLOCKED

## Respostas técnicas

1. Estabilidade do firmware: long runs C host `COMPLETED`; Zephyr `PASSED`.
2. Coerência dos periféricos virtuais: Renode `PASSED`; Renode platform smoke and Zephyr ELF execution completed.
3. Energia digital estimada na janela observada: `0.514252 µAh` pela integração SIMULATED de correntes ASSUMED/trace; ngspice: EXECUTED.
4. Estabilidade do rail: ngspice `EXECUTED`; sem medição física ou resultado de rail quando não executado.
5. Evento com maior carga integrada: `TX` / `sx1262` (0.375 µAh) na janela simulada.
6. O envelope mecânico estimado cabe: análise de caixas delimitadoras `COMPLETED`; sem conflito de envelope reportado.
7. Frequência de ressonância/S11: openEMS `PARTIAL_OR_BLOCKED`; 1/5 cenários passaram a comparação numérica de malha; resultados simulados não validam desempenho físico.
8. Degradação por PCB/bateria/carcaça/animal: 5 cenários listados; resultados exigem openEMS; aproximação animal é experimental.
9. Encaixe geométrico estimado: `PASS`; CadQuery disponível `True`. O resultado não valida montagem física.
10. Falhas encontradas: 7 entradas; falhas de host cobertas `one_shot_i2c_failure_recovery, one_shot_spi_failure_recovery, late_tx_done_timeout_recovery, digital_fault_crc_corruption, digital_fault_sx1262_busy_stuck, digital_fault_irq_missing, digital_reset_cause_flag_classification`; pendentes `battery_voltage_drop, high_esr, regulator_instability, watchdog_reset_executed_on_target, unexpected_reboot`.
11. Hipóteses a revisar: parâmetros ASSUMED e limites provisórios em hardware/spec.yaml; dimensões, antena e encaixe aguardam aprovação.
12. Parâmetros por status: `{"ASSUMED": 78, "DATASHEET": 18, "SIMULATED": 3}`; provenance completa na spec.
13. Sem hardware real não são validados consumo, brownout, potência RF, sintonia, materiais ou comportamento animal.
14. Este gate não é validação comercial, clínica ou de campo.

## Integridade da evidência

Nenhum campo MEASURED é permitido na spec do MVP2. Capacidades GPU são metadados de ambiente e o experimento Sionna é opcional.
