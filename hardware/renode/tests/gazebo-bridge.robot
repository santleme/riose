*** Settings ***
Documentation     Firmware reads Gazebo-derived SIMULATED RESD samples and exposes a boot-only logical TX payload.

*** Variables ***
${PLATFORM}       ${CURDIR}/../riose_stm32l0.repl
${ELF}            %{RIOSE_ZEPHYR_ELF=}
${GAZEBO_RESD}    %{RIOSE_LIS2DW12_GAZEBO_RESD=}
${TX_TRACE_OUTPUT}    %{RIOSE_RENODE_TX_TRACE=}

*** Test Cases ***
Firmware Reads Gazebo RESD Samples
    Skip If    '${ELF}' == ''    Set RIOSE_ZEPHYR_ELF to the Renode-profile Zephyr ELF.
    Skip If    '${GAZEBO_RESD}' == ''    Set RIOSE_LIS2DW12_GAZEBO_RESD to the bridge-generated GAZEBO RESD file.
    Execute Command    sysbus.i2c1.imu Write [0x20, 0x24]
    Execute Command    sysbus.i2c1.imu FinishTransmission
    Execute Command    sysbus.i2c1.imu FeedAccelerationSamplesFromRESD @${GAZEBO_RESD}
    Execute Command    sysbus LoadELF @${ELF}
    Execute Command    emulation RunFor "0.25"
    ${reads}=    Execute Command    sysbus.i2c1.imu OutputSampleReadCount
    Should Be True    int($reads.strip(), 0) > 0
    ${payload}=    Evaluate    pathlib.Path($gazebo_resd).read_bytes()    modules=pathlib
    ${metadata_size}=    Evaluate    struct.unpack_from('<Q', $payload, 37)[0]    modules=struct
    ${ug}=    Evaluate    struct.unpack_from('<iii', $payload, 45 + $metadata_size)    modules=struct
    FOR    ${axis}    ${index}    IN    X    0    Y    1    Z    2
        ${actual}=    Execute Command    sysbus.i2c1.imu LastOutput${axis}Raw
        ${actual}=    Evaluate    (int($actual.strip(), 0) + 2**31) % 2**32 - 2**31
        ${expected}=    Evaluate    int($ug[int($index)] / 244) * 4
        Should Be Equal As Integers    ${actual}    ${expected}
    END
    [Setup]    Create RIOSE Platform

Firmware Startup TX Payload Is Captured
    Skip If    '${ELF}' == ''    Set RIOSE_ZEPHYR_ELF to the Renode-profile Zephyr ELF.
    # A stationary 1 g sample provides deterministic boot-time input. This
    # checks startup telemetry only; it does not represent movement wake.
    Execute Command    sysbus.i2c1.imu DefaultAccelerationZ 1
    Execute Command    sysbus.i2c1.imu AccelerationZ 1
    Execute Command    sysbus LoadELF @${ELF}
    Execute Command    emulation RunFor "0.25"
    ${tx_count}=    Execute Command    sysbus.spi1.radio TxCount
    Should Be Equal As Integers    ${tx_count}    1
    ${tx_done_count}=    Execute Command    sysbus.spi1.radio TxDoneCount
    Should Be Equal As Integers    ${tx_done_count}    1
    ${payload_length}=    Execute Command    sysbus.spi1.radio LastTxPayloadLength
    Should Be Equal As Integers    ${payload_length}    24
    ${payload_hex}=    Execute Command    sysbus.spi1.radio LastTxPayloadHex
    ${payload}=    Evaluate    bytes.fromhex($payload_hex.strip())
    ${version}=    Evaluate    $payload[0]
    ${tag_id}=    Evaluate    int.from_bytes($payload[2:6], 'little')
    ${sequence}=    Evaluate    int.from_bytes($payload[6:10], 'little')
    ${behavior}=    Evaluate    $payload[1]
    ${x_mg}=    Evaluate    int.from_bytes($payload[14:16], 'little', signed=True)
    ${y_mg}=    Evaluate    int.from_bytes($payload[16:18], 'little', signed=True)
    ${battery_mv}=    Evaluate    int.from_bytes($payload[20:22], 'little')
    ${crc_valid}=    Evaluate    binascii.crc_hqx($payload[:22], 0xFFFF) == int.from_bytes($payload[22:24], 'little')    modules=binascii
    Should Be Equal As Integers    ${version}    1
    Should Be Equal As Integers    ${tag_id}    1
    Should Be Equal As Integers    ${sequence}    0
    Should Be True    0 <= ${behavior} <= 3
    Should Be Equal As Integers    ${x_mg}    0
    Should Be Equal As Integers    ${y_mg}    0
    Should Be Equal As Integers    ${battery_mv}    3000
    Should Be True    ${crc_valid}
    [Setup]    Create RIOSE Platform

LIS2DW12 Accepts Individual Gazebo Acceleration Samples
    Execute Command    sysbus.i2c1.imu Write [0x20, 0x14]
    Execute Command    sysbus.i2c1.imu FinishTransmission
    Execute Command    sysbus.i2c1.imu Write [0x23, 0x20]
    Execute Command    sysbus.i2c1.imu FinishTransmission
    Execute Command    sysbus.i2c1.imu Write [0x34, 0x02]
    Execute Command    sysbus.i2c1.imu FinishTransmission
    Execute Command    sysbus.i2c1.imu Write [0x35, 0x00]
    Execute Command    sysbus.i2c1.imu FinishTransmission
    Execute Command    sysbus.i2c1.imu Write [0x3F, 0x20]
    Execute Command    sysbus.i2c1.imu FinishTransmission
    Execute Command    sysbus.i2c1.imu InjectAccelerationSampleFromGazebo 0 0 1
    Execute Command    sysbus.i2c1.imu InjectAccelerationSampleFromGazebo 0.575 0.793 0
    ${sample_count}=    Execute Command    sysbus.i2c1.imu GazeboSampleInjectionCount
    Should Be Equal As Integers    ${sample_count}    2
    ${irq}=    Execute Command    sysbus.i2c1.imu WakeupIRQAsserted
    Should Be Equal    ${irq.strip()}    True
    Execute Command    sysbus.i2c1.imu Write [0x38]
    ${source}=    Execute Command    sysbus.i2c1.imu Read 1
    Should Contain    ${source}    0F
    Execute Command    sysbus.i2c1.imu FinishTransmission
    ${irq_after_read}=    Execute Command    sysbus.i2c1.imu WakeupIRQAsserted
    Should Be Equal    ${irq_after_read.strip()}    False
    [Setup]    Create RIOSE Platform

Firmware Wakes From Gazebo Motion Comparator And Transmits
    Skip If    '${ELF}' == ''    Set RIOSE_ZEPHYR_ELF to the Renode-profile Zephyr ELF.
    Skip If    '${GAZEBO_RESD}' == ''    Set RIOSE_LIS2DW12_GAZEBO_RESD to the bridge-generated movement RESD file.
    # Start from production firmware register configuration and let movement
    # samples pass through the configured WU comparator model.
    Execute Command    sysbus.i2c1.imu DefaultAccelerationZ 1
    Execute Command    sysbus.i2c1.imu AccelerationZ 1
    Execute Command    sysbus LoadELF @${ELF}
    Execute Command    emulation RunFor "0.25"
    ${initial_tx}=    Execute Command    sysbus.spi1.radio TxCount
    Should Be Equal As Integers    ${initial_tx}    1
    Firmware State Should Be    2
    ${ctrl1}=    Execute Command    sysbus.i2c1.imu Control1Configuration
    Should Be Equal As Integers    ${ctrl1}    0x14    base=16
    ${sample_rate}=    Execute Command    sysbus.i2c1.imu SampleRate
    Should Be Equal As Integers    ${sample_rate}    13
    # At emulation time 0.25 s, align the trace's first Gazebo sample with
    # the current Renode timestamp instead of seeking 0.25 s into the motion.
    Execute Command    sysbus.i2c1.imu FeedAccelerationSamplesFromRESD @${GAZEBO_RESD} sampleOffsetTime=-250000000
    # Include the firmware's configured 100 ms RX window after TX_DONE, then
    # observe its transition back to SLEEP.
    Execute Command    emulation RunFor "0.50"
    ${tx_count}=    Execute Command    sysbus.spi1.radio TxCount
    Should Be Equal As Integers    ${tx_count}    2
    ${tx_done}=    Execute Command    sysbus.spi1.radio TxDoneCount
    Should Be Equal As Integers    ${tx_done}    2
    ${sample_reads}=    Execute Command    sysbus.i2c1.imu OutputSampleReadCount
    Should Be Equal As Integers    ${sample_reads}    3
    ${wu_source_reads}=    Execute Command    sysbus.i2c1.imu WakeupSourceReadCount
    Should Be True    int($wu_source_reads.strip(), 0) > 0
    ${wu_events}=    Execute Command    sysbus.i2c1.imu WakeupGeneratedEventCount
    Should Be Equal As Integers    ${wu_events}    1
    ${wu_event_reads}=    Execute Command    sysbus.i2c1.imu WakeupEventReadCount
    Should Be Equal As Integers    ${wu_event_reads}    1
    ${wu_source}=    Execute Command    sysbus.i2c1.imu LastWakeupSourceValue
    ${wu_source}=    Evaluate    int($wu_source.strip(), 0)
    Should Be True    ($wu_source & 0x08) != 0
    Should Be True    ($wu_source & 0x07) != 0
    ${wu_irq}=    Execute Command    sysbus.i2c1.imu WakeupIRQAsserted
    ${wu_irq}=    Strip String    ${wu_irq}
    Should Be Equal    ${wu_irq}    False
    ${payload_length}=    Execute Command    sysbus.spi1.radio LastTxPayloadLength
    Should Be Equal As Integers    ${payload_length}    24
    ${payload_hex}=    Execute Command    sysbus.spi1.radio LastTxPayloadHex
    ${packet}=    Evaluate    bytes.fromhex($payload_hex.strip())
    ${behavior}=    Evaluate    $packet[1]
    ${x_mg}=    Evaluate    int.from_bytes($packet[14:16], 'little', signed=True)
    ${y_mg}=    Evaluate    int.from_bytes($packet[16:18], 'little', signed=True)
    ${z_mg}=    Evaluate    int.from_bytes($packet[18:20], 'little', signed=True)
    ${crc_valid}=    Evaluate    binascii.crc_hqx($packet[:22], 0xFFFF) == int.from_bytes($packet[22:24], 'little')    modules=binascii
    Firmware State Should Be    2
    Log To Console    movement WU evidence: tx=${tx_count.strip()} completed=${tx_done.strip()} wake_events=${wu_events.strip()} source_reads=${wu_event_reads.strip()} samples_read=${sample_reads.strip()} behavior=${behavior} imu_mg=(${x_mg},${y_mg},${z_mg}) WU_SRC=${wu_source} WU_IRQ=${wu_irq.strip()} CRC=${crc_valid} final_state=SLEEP
    Should Be Equal As Integers    ${behavior}    3
    Should Be Equal As Integers    ${x_mg}    575
    Should Be Equal As Integers    ${y_mg}    793
    Should Be Equal As Integers    ${z_mg}    0
    Should Be True    ${crc_valid}
    ${trace_json}=    Execute Command    sysbus.spi1.radio TxTraceJson
    ${trace}=    Evaluate    json.loads($trace_json)    modules=json
    Should Be Equal    ${trace}[schema_version]    riose.renode.sx1262_tx_trace/v1
    Should Be Equal    ${trace}[clock]    Machine.ElapsedVirtualTime.TimeElapsed
    Should Be Equal    ${trace}[time_unit]    ns
    ${records}=    Set Variable    ${trace}[records]
    ${record_count}=    Get Length    ${records}
    Should Be Equal As Integers    ${record_count}    2
    ${first_record}=    Set Variable    ${records}[0]
    ${second_record}=    Set Variable    ${records}[1]
    Should Be Equal As Integers    ${first_record}[tx_index]    1
    Should Be Equal As Integers    ${second_record}[tx_index]    2
    FOR    ${record}    IN    @{records}
        Should Be Equal    ${record}[status]    completed
        Should Be True    ${record}[payload_captured]
        Should Be Equal As Integers    ${record}[payload_length]    24
        Should Be Equal As Integers    ${record}[rf_frequency_word]    959447040
        # Renode uses the Nucleo L031K6 firmware config (14 dBm).
        Should Be Equal As Integers    ${record}[tx_power_dbm]    14
        Should Not Be Equal    ${record}[tx_done_ns]    ${None}
        Should Be True    int($record["tx_done_ns"]) >= int($record["tx_start_ns"])
        ${record_payload}=    Evaluate    bytes.fromhex($record["payload_hex"])
        ${record_crc}=    Evaluate    binascii.crc_hqx($record_payload[:22], 0xFFFF) == int.from_bytes($record_payload[22:24], 'little')    modules=binascii
        Should Be True    ${record_crc}
        Should Be Equal As Integers    ${record_payload}[0]    1
        Should Be Equal As Integers    ${record_payload}[2]    1
    END
    Should Be True    int($first_record["tx_start_ns"]) < int($second_record["tx_start_ns"])
    IF    '${TX_TRACE_OUTPUT}' != ''
        Evaluate    pathlib.Path($tx_trace_output).parent.mkdir(parents=True, exist_ok=True)    modules=pathlib
        Evaluate    pathlib.Path($tx_trace_output).write_text($trace_json, encoding='utf-8')    modules=pathlib
        Log To Console    SX1262 TX trace exported to ${TX_TRACE_OUTPUT}
    END
    [Setup]    Create RIOSE Platform

*** Keywords ***
Create RIOSE Platform
    Execute Command    mach create
    Execute Command    machine LoadPlatformDescription @${PLATFORM}

Firmware State Should Be
    [Arguments]    ${expected}
    ${odr}=    Execute Command    sysbus ReadDoubleWord 0x50000014
    ${raw}=    Evaluate    int($odr.strip(), 0)
    ${state}=    Evaluate    ($raw & 0x03) | (($raw & 0x08) >> 1)
    Should Be Equal As Integers    ${state}    ${expected}
