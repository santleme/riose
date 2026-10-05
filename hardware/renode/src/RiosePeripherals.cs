// Deterministic digital models for Renode. These are protocol-level models,
// not electrical or RF simulations. Behavior is intentionally scoped to the
// commands/registers exercised by the RIOSE Zephyr firmware.
using System;
using System.Collections.Generic;
using System.Globalization;
using System.Reflection;
using System.Text;
using Antmicro.Renode.Core;
using Antmicro.Renode.Core.Structure;
using Antmicro.Renode.Peripherals;
using Antmicro.Renode.Peripherals.GPIOPort;
using Antmicro.Renode.Peripherals.I2C;
using Antmicro.Renode.Peripherals.SPI;
using Antmicro.Renode.Peripherals.Timers;
using Antmicro.Renode.Time;
using Antmicro.Renode.Utilities.RESD;

namespace Antmicro.Renode.Peripherals.Riose
{
    public sealed class SX1262 : ISPIPeripheral, IGPIOReceiver
    {
        private const int FifoSize = 256;
        private const byte CmdOk = 0x02;
        private const byte CmdDataAvailable = 0x04;
        private const byte CmdTimeout = 0x06;
        private const byte CmdInvalid = 0x08;
        private const byte CmdFailed = 0x0A;
        private const ushort IrqTxDone = 0x0001;
        private const ushort IrqTimeout = 0x0200;
        private const ulong TicksPerMillisecond = 64; // SX126x timeout ticks are 15.625 us.
        private const ulong ContinuousRxWindowTicks = 64000; // One documented virtual second.

        private readonly byte[] fifo = new byte[FifoSize];
        private readonly List<byte> txFrame = new List<byte>();
        private readonly byte[] modulation = new byte[4];
        private readonly byte[] packetParams = new byte[9];
        private readonly byte[] paConfig = new byte[4];
        private readonly byte[] imageCalibration = new byte[2];
        private readonly LimitTimer operationTimer;
        private readonly Machine machine;
        private readonly List<TxTraceRecord> txTraceRecords = new List<TxTraceRecord>();
        private TxTraceRecord activeTxTraceRecord;
        private uint txTraceIndex;
        private int txLength;
        private byte opcode;
        private byte mode = 0x20; // standby RC
        private byte commandStatus = CmdOk;
        private ushort irqStatus;
        private ushort irqMask;
        private ushort dio1Mask;
        private ushort dio2Mask;
        private ushort dio3Mask;
        private uint txLatencyMs = 30;
        private uint txCount;
        private uint txDoneCount;
        private uint rxCount;
        private uint rxTimeoutCount;
        private uint faultCount;
        private uint rfFrequencyWord;
        private uint lastTxPayloadLength;
        private string lastTxPayloadHex = "";
        private byte packetType;
        private byte txPower;
        private byte rampTime;
        private byte txBase;
        private byte rxBase;
        private bool selected;
        private bool operationIsRx;
        private bool operationTimesOut;
        private bool holdBusy;
        private bool suppressIRQ;
        private bool dropSPI;
        private bool dio2RfSwitchEnabled;

        public SX1262(Machine machine)
        {
            this.machine = machine;
            Busy = new GPIO();
            IRQ = new GPIO();
            operationTimer = new LimitTimer(machine.ClockSource, 64000, this, "SX1262 operation",
                limit: 1, eventEnabled: true, direction: Direction.Ascending,
                enabled: false, autoUpdate: false, workMode: WorkMode.OneShot);
            operationTimer.LimitReached += CompleteOperation;
            Reset();
        }

        public GPIO Busy { get; }
        public GPIO IRQ { get; }

        // Fault hooks can be controlled from Renode scripts and Robot tests.
        public bool HoldBusy { get => holdBusy; set { holdBusy = value; UpdatePins(); } }
        public bool SuppressIRQ { get => suppressIRQ; set { suppressIRQ = value; UpdateIRQ(); } }
        public bool DropSPI { get => dropSPI; set => dropSPI = value; }
        public uint TxLatencyMs { get => txLatencyMs; set => txLatencyMs = Math.Max(1u, value); }
        public uint FaultCount => faultCount;
        public uint TxCount => txCount;
        public uint TxDoneCount => txDoneCount;
        public uint RxCount => rxCount;
        public uint RxTimeoutCount => rxTimeoutCount;
        public byte LastOpcode => opcode;
        public uint LastTxPayloadLength => lastTxPayloadLength;
        public string LastTxPayloadHex => lastTxPayloadHex;
        public string TxTraceJson => SerializeTxTrace();
        public byte CurrentMode => mode;
        public ushort IRQStatus => irqStatus;
        public uint RfFrequencyWord => rfFrequencyWord;
        public byte PacketType => packetType;
        public byte TxBase => txBase;
        public byte RxBase => rxBase;
        public bool Dio2RfSwitchEnabled => dio2RfSwitchEnabled;
        public bool BusyAsserted => HoldBusy || mode == 0x60 || mode == 0x50;
        public bool IRQAsserted => !SuppressIRQ && (irqStatus & irqMask & dio1Mask) != 0;

        public byte Transmit(byte data)
        {
            if(DropSPI) return 0xFF;
            if(!selected)
            {
                if(data != 0xC0) commandStatus = CmdOk;
                selected = true;
                txLength = 0;
                opcode = data;
                txFrame.Clear();
                txFrame.Add(data);
                txLength++;
                return 0;
            }

            byte result = 0;
            if(opcode == 0xC0) result = Status();
            else if(opcode == 0x12)
            {
                if(txLength == 1) result = Status();
                else if(txLength == 2) result = (byte)(irqStatus >> 8);
                else if(txLength == 3) result = (byte)irqStatus;
            }
            else if(opcode == 0x1E && txLength >= 3)
                result = fifo[(byte)(txFrame[1] + txLength - 3)];

            txFrame.Add(data);
            txLength++;
            return result;
        }

        public void FinishTransmission()
        {
            if(!selected) return;
            selected = false;
            if(DropSPI) return;

            switch(opcode)
            {
                case 0xC0: // GetStatus
                    if(RequireLength(2)) commandStatus = CmdOk;
                    break;
                case 0x84: // SetSleep
                    if(RequireLength(2))
                    {
                        mode = 0x00;
                        CancelOperation();
                    }
                    break;
                case 0x80: // SetStandby
                    if(RequireLength(2))
                    {
                        if(txFrame[1] > 1) Fault(CmdInvalid);
                        else
                        {
                            mode = txFrame[1] == 1 ? (byte)0x30 : (byte)0x20;
                            CancelOperation();
                        }
                    }
                    break;
                case 0x8A: // SetPacketType
                    if(RequireLength(2))
                    {
                        if(txFrame[1] > 1) Fault(CmdInvalid);
                        else packetType = txFrame[1];
                    }
                    break;
                case 0x9D: // SetDio2AsRfSwitchCtrl
                    if(RequireLength(2))
                    {
                        if(txFrame[1] > 1) Fault(CmdInvalid);
                        else dio2RfSwitchEnabled = txFrame[1] != 0;
                    }
                    break;
                case 0x86: // SetRfFrequency
                    if(RequireLength(5)) rfFrequencyWord = ReadU32(1);
                    break;
                case 0x8E: // SetTxParams
                    if(RequireLength(3)) { txPower = txFrame[1]; rampTime = txFrame[2]; }
                    break;
                case 0x98: // CalibrateImage
                    if(RequireLength(3)) { imageCalibration[0] = txFrame[1]; imageCalibration[1] = txFrame[2]; }
                    break;
                case 0x95: // SetPaConfig
                    if(RequireLength(5))
                    {
                        if(txFrame[1] > 0x07 || txFrame[2] > 0x07 || txFrame[3] > 1 || txFrame[4] != 1)
                            Fault(CmdInvalid);
                        else
                            for(int i = 0; i < paConfig.Length; i++) paConfig[i] = txFrame[i + 1];
                    }
                    break;
                case 0x8B: // SetModulationParams
                    if(RequireLength(5))
                    {
                        if(packetType == 1 && (txFrame[1] < 5 || txFrame[1] > 12 || !ValidLoRaBandwidth(txFrame[2]) ||
                            txFrame[3] < 1 || txFrame[3] > 4 || txFrame[4] > 1)) Fault(CmdInvalid);
                        else
                            for(int i = 0; i < modulation.Length; i++) modulation[i] = txFrame[i + 1];
                    }
                    break;
                case 0x8C: // SetPacketParams (LoRa: six args; GFSK: nine args)
                    if(RequireLength(packetType == 1 ? 7 : 10))
                    {
                        Array.Clear(packetParams, 0, packetParams.Length);
                        for(int i = 1; i < txLength; i++) packetParams[i - 1] = txFrame[i];
                    }
                    break;
                case 0x8F: // SetBufferBaseAddress
                    if(RequireLength(3)) { txBase = txFrame[1]; rxBase = txFrame[2]; }
                    break;
                case 0x08: // SetDioIrqParams
                    if(RequireLength(9))
                    {
                        irqMask = ReadU16(1);
                        dio1Mask = ReadU16(3);
                        dio2Mask = ReadU16(5);
                        dio3Mask = ReadU16(7);
                        UpdateIRQ();
                    }
                    break;
                case 0x02: // ClearIrqStatus
                    if(RequireLength(3)) { irqStatus &= (ushort)~ReadU16(1); UpdateIRQ(); }
                    break;
                case 0x0E: // WriteBuffer
                    if(RequireAtLeastLength(2))
                        for(int i = 2; i < txLength; i++) fifo[(byte)(txFrame[1] + i - 2)] = txFrame[i];
                    break;
                case 0x1E: // ReadBuffer
                    if(RequireAtLeastLength(3)) commandStatus = CmdDataAvailable;
                    break;
                case 0x12: // GetIrqStatus
                    if(RequireLength(4)) commandStatus = CmdDataAvailable;
                    break;
                case 0x83: // SetTx
                    StartOperation(isRx: false);
                    break;
                case 0x82: // SetRx
                    StartOperation(isRx: true);
                    break;
                default:
                    Fault(CmdInvalid);
                    break;
            }
            UpdatePins();
        }

        public void OnGPIO(int number, bool value)
        {
            // GPIO 0 is the external active-low reset; GPIO 1 is active-low
            // chip select, so a high level ends the current SPI command.
            if(number == 0 && !value) Reset();
            else if(number == 1 && value) FinishTransmission();
        }

        public void Reset()
        {
            if(activeTxTraceRecord != null)
            {
                activeTxTraceRecord.Status = "aborted_by_reset";
                activeTxTraceRecord = null;
            }
            Array.Clear(fifo, 0, fifo.Length);
            Array.Clear(modulation, 0, modulation.Length);
            Array.Clear(packetParams, 0, packetParams.Length);
            Array.Clear(paConfig, 0, paConfig.Length);
            Array.Clear(imageCalibration, 0, imageCalibration.Length);
            txFrame.Clear();
            operationTimer.Reset();
            mode = 0x20;
            commandStatus = CmdOk;
            irqStatus = irqMask = dio1Mask = dio2Mask = dio3Mask = 0;
            txCount = txDoneCount = rxCount = rxTimeoutCount = faultCount = rfFrequencyWord = 0;
            lastTxPayloadLength = 0;
            lastTxPayloadHex = "";
            packetType = txPower = rampTime = txBase = rxBase = 0;
            selected = operationIsRx = operationTimesOut = false;
            holdBusy = suppressIRQ = dropSPI = dio2RfSwitchEnabled = false;
            UpdatePins();
        }

        private byte Status() => (byte)(mode | (commandStatus & 0x0E));

        private bool RequireLength(int expected)
        {
            if(txLength == expected) return true;
            Fault(CmdInvalid);
            return false;
        }

        private bool ValidLoRaBandwidth(byte bandwidth) => bandwidth == 0x00 || bandwidth == 0x01 || bandwidth == 0x02 ||
            bandwidth == 0x03 || bandwidth == 0x04 || bandwidth == 0x05 || bandwidth == 0x06 || bandwidth == 0x09 ||
            bandwidth == 0x0A || bandwidth == 0x0B;

        private bool RequireAtLeastLength(int minimum)
        {
            if(txLength >= minimum) return true;
            Fault(CmdInvalid);
            return false;
        }

        private void Fault(byte status)
        {
            commandStatus = status;
            faultCount++;
        }

        private ushort ReadU16(int offset) => (ushort)((txFrame[offset] << 8) | txFrame[offset + 1]);

        private uint ReadU32(int offset) => ((uint)txFrame[offset] << 24) | ((uint)txFrame[offset + 1] << 16) |
            ((uint)txFrame[offset + 2] << 8) | txFrame[offset + 3];

        private ulong ReadTimeoutTicks() => ((ulong)txFrame[1] << 16) | ((ulong)txFrame[2] << 8) | txFrame[3];

        private void StartOperation(bool isRx)
        {
            if(txLength != 4 || mode == 0x00 || mode == 0x60 || HoldBusy)
            {
                Fault(txLength == 4 ? CmdFailed : CmdInvalid);
                return;
            }

            operationIsRx = isRx;
            if(isRx) rxCount++;
            ulong requestedTicks = ReadTimeoutTicks();
            ulong latencyTicks = (ulong)TxLatencyMs * TicksPerMillisecond;
            if(isRx && requestedTicks == 0) requestedTicks = ContinuousRxWindowTicks;
            operationTimesOut = isRx
                ? requestedTicks != 0
                : requestedTicks != 0 && requestedTicks <= latencyTicks;
            ulong eventTicks = isRx
                ? requestedTicks
                : operationTimesOut ? requestedTicks : latencyTicks;
            operationTimer.Reset();
            operationTimer.Limit = Math.Max(1UL, eventTicks);
            mode = isRx ? (byte)0x50 : (byte)0x60;
            operationTimer.Enabled = true;
            if(!isRx)
            {
                CaptureTxPayload();
                txCount++;
                activeTxTraceRecord = new TxTraceRecord(++txTraceIndex,
                    machine.ElapsedVirtualTime.TimeElapsed.Ticks,
                    lastTxPayloadLength, lastTxPayloadHex, rfFrequencyWord, unchecked((sbyte)txPower));
                txTraceRecords.Add(activeTxTraceRecord);
            }
        }

        private void CaptureTxPayload()
        {
            // For LoRa packet parameters, payload length is the fourth argument
            // (packetParams[3]); preserve exactly the bytes currently staged in
            // the virtual SX1262 FIFO for deterministic integration evidence.
            var length = packetType == 1 ? packetParams[3] : (byte)0;
            lastTxPayloadLength = length;
            var payload = new char[length * 2];
            const string hex = "0123456789abcdef";
            for(var i = 0; i < length; i++)
            {
                var value = fifo[(byte)(txBase + i)];
                payload[i * 2] = hex[value >> 4];
                payload[i * 2 + 1] = hex[value & 0x0F];
            }
            lastTxPayloadHex = new string(payload);
        }

        private string SerializeTxTrace()
        {
            var result = new StringBuilder();
            result.Append("{\"schema_version\":\"riose.renode.sx1262_tx_trace/v1\",");
            result.Append("\"clock\":\"Machine.ElapsedVirtualTime.TimeElapsed\",");
            result.Append("\"time_unit\":\"ns\",\"provenance\":\"SIMULATED\",\"records\":[");
            for(var i = 0; i < txTraceRecords.Count; i++)
            {
                if(i > 0) result.Append(',');
                var record = txTraceRecords[i];
                result.Append("{\"tx_index\":").Append(record.TxIndex.ToString(CultureInfo.InvariantCulture));
                result.Append(",\"tx_start_ns\":").Append(record.TxStartNanoseconds.ToString(CultureInfo.InvariantCulture));
                result.Append(",\"tx_done_ns\":");
                result.Append(record.TxDoneNanoseconds.HasValue
                    ? record.TxDoneNanoseconds.Value.ToString(CultureInfo.InvariantCulture)
                    : "null");
                result.Append(",\"payload_length\":").Append(record.PayloadLength.ToString(CultureInfo.InvariantCulture));
                result.Append(",\"payload_hex\":\"").Append(record.PayloadHex).Append('"');
                result.Append(",\"payload_captured\":").Append(record.PayloadLength > 0 ? "true" : "false");
                result.Append(",\"rf_frequency_word\":").Append(record.RfFrequencyWord.ToString(CultureInfo.InvariantCulture));
                result.Append(",\"tx_power_dbm\":").Append(record.TxPowerDbm.ToString(CultureInfo.InvariantCulture));
                result.Append(",\"status\":\"").Append(record.Status).Append("\"}");
            }
            result.Append("]}");
            return result.ToString();
        }

        private sealed class TxTraceRecord
        {
            public TxTraceRecord(uint txIndex, ulong txStartNanoseconds, uint payloadLength, string payloadHex,
                uint rfFrequencyWord, sbyte txPowerDbm)
            {
                TxIndex = txIndex;
                TxStartNanoseconds = txStartNanoseconds;
                PayloadLength = payloadLength;
                PayloadHex = payloadHex;
                RfFrequencyWord = rfFrequencyWord;
                TxPowerDbm = txPowerDbm;
                Status = "in_progress";
            }

            public uint TxIndex { get; }
            public ulong TxStartNanoseconds { get; }
            public ulong? TxDoneNanoseconds { get; set; }
            public uint PayloadLength { get; }
            public string PayloadHex { get; }
            public uint RfFrequencyWord { get; }
            public sbyte TxPowerDbm { get; }
            public string Status { get; set; }
        }

        private void CompleteOperation()
        {
            mode = 0x20;
            irqStatus |= operationTimesOut ? IrqTimeout : operationIsRx ? IrqTimeout : IrqTxDone;
            if(!operationIsRx)
            {
                if(!operationTimesOut)
                {
                    txDoneCount++;
                    if(activeTxTraceRecord != null)
                    {
                        activeTxTraceRecord.TxDoneNanoseconds = machine.ElapsedVirtualTime.TimeElapsed.Ticks;
                        activeTxTraceRecord.Status = "completed";
                    }
                }
                else if(activeTxTraceRecord != null)
                {
                    activeTxTraceRecord.Status = "timed_out";
                }
                activeTxTraceRecord = null;
            }
            if(operationIsRx) rxTimeoutCount++;
            commandStatus = operationTimesOut ? CmdTimeout : CmdOk;
            UpdatePins();
        }

        private void CancelOperation()
        {
            operationTimer.Reset();
            operationIsRx = operationTimesOut = false;
            UpdatePins();
        }

        private void UpdateIRQ()
        {
            IRQ.Set(!SuppressIRQ && (irqStatus & irqMask & dio1Mask) != 0);
        }

        private void UpdatePins()
        {
            Busy.Set(HoldBusy || mode == 0x60 || mode == 0x50);
            UpdateIRQ();
        }
    }

    // Extends Renode's upstream LIS2DW12 data/RESD model with a sampled wake
    // comparator. The threshold and duration follow the configured register
    // fields; the high-pass filter is an explicit first-order digital
    // approximation, not a transistor-level or silicon-validated model.
    public sealed class LIS2DW12WakeModel : Sensors.LIS2DW12, II2CPeripheral, IGPIOReceiver
    {
        public LIS2DW12WakeModel(IMachine machine) : base(machine)
        {
            // The upstream implementation initializes the output scale only
            // from Reset(); do so explicitly for platforms that do not reset
            // devices during construction before the first sample-register read.
            base.Reset();
            // The native data-ready GPIO is an input to the adapter. Keep a
            // separate exposed pin so native updates cannot pulse PA8 low
            // while a simulated wake source is still latched.
            base.Interrupt1.Connect(this, 0);
            sampleRateSetter = RequireSampleRateSetter();
        }

        public new GPIO Interrupt1 { get; } = new GPIO();
        public bool WakeupIRQAsserted => wakeupIRQAsserted;
        public uint WakeupEventReadCount => wakeupEventReadCount;
        public uint WakeupGeneratedEventCount => wakeupGeneratedEventCount;
        public uint WakeupSourceReadCount => wakeupSourceReadCount;
        public byte LastWakeupSourceValue => lastWakeupSourceValue;
        public byte Control1Configuration => control1;
        public uint OutputSampleReadCount => outputSampleReadCount;
        public uint GazeboSampleInjectionCount => gazeboSampleInjectionCount;
        public uint WakeupComparatorSampleCount => wakeupComparatorSampleCount;
        public int LastOutputXRaw => lastOutputXRaw;
        public int LastOutputYRaw => lastOutputYRaw;
        public int LastOutputZRaw => lastOutputZRaw;

        // Host-side lockstep bridges can inject one Gazebo sample without
        // prebuilding/reloading a RESD file. Values use g, matching the
        // upstream FeedAccelerationSample API and the LIS2DW12 register path.
        public void InjectAccelerationSampleFromGazebo(decimal xG, decimal yG, decimal zG)
        {
            base.FeedAccelerationSample(xG, yG, zG);
            gazeboSampleInjectionCount++;
            EvaluateWakeup(xG, yG, zG);
            UpdateWakeupIRQ();
        }

        // Live lockstep keeps Gazebo's raw sensor stream at 50 Hz while the
        // wake comparator samples the held sensor value at configured ODR.
        // timestampSeconds is the Renode virtual time when this sample arrives.
        public void InjectAccelerationSampleFromGazeboAtTime(
            decimal xG, decimal yG, decimal zG, decimal timestampSeconds)
        {
            if(timestampSeconds < 0m)
                throw new ArgumentOutOfRangeException(nameof(timestampSeconds));
            base.FeedAccelerationSample(xG, yG, zG);
            gazeboSampleInjectionCount++;

            var odrHz = ConfiguredOutputRateHz();
            if(odrHz <= 0m)
            {
                filterInitialized = false;
                thresholdSamples = 0;
                hasGazeboTimelineSample = true;
                UpdateWakeupIRQ();
                return;
            }
            if(!hasGazeboTimelineSample)
            {
                // There is no comparator history before the first raw sample.
                nextWakeupSampleTime = 1m / odrHz;
                hasGazeboTimelineSample = true;
            }
            var period = 1m / odrHz;
            while(nextWakeupSampleTime <= timestampSeconds + 0.000000001m)
            {
                EvaluateWakeup(xG, yG, zG);
                nextWakeupSampleTime += period;
            }
            UpdateWakeupIRQ();
        }

        public void OnGPIO(int number, bool value)
        {
            if(number != 0) return;
            upstreamIRQAsserted = value;
            UpdateWakeupIRQ();
        }

        // RESD discovers callbacks on the concrete runtime type. The upstream
        // callbacks are private and therefore are not inherited by this shim.
        // Forward to them so native FIFO, defaults and end-of-stream semantics
        // remain owned by Renode rather than duplicating its sample pipeline.
        [OnRESDSample(SampleType.Acceleration)]
        [BeforeRESDSample(SampleType.Acceleration)]
        private void HandleRESDAcceleration(AccelerationSample sample, TimeInterval timestamp)
        {
            upstreamAccelerationHandler.Invoke(this, new object[] { sample, timestamp });
            if(sample != null)
            {
                EvaluateWakeup(sample.AccelerationX / 1e6m, sample.AccelerationY / 1e6m,
                               sample.AccelerationZ / 1e6m);
            }
            UpdateWakeupIRQ();
        }

        [AfterRESDSample(SampleType.Acceleration)]
        private void HandleRESDAccelerationEnded(AccelerationSample sample, TimeInterval timestamp)
        {
            upstreamAccelerationEndedHandler.Invoke(this, new object[] { sample, timestamp });
            UpdateWakeupIRQ();
        }

        private static MethodInfo RequireUpstreamHandler(string name)
        {
            var handler = typeof(Sensors.LIS2DW12).GetMethod(name,
                BindingFlags.Instance | BindingFlags.NonPublic, null,
                new[] { typeof(AccelerationSample), typeof(TimeInterval) }, null);
            if(handler == null)
            {
                throw new InvalidOperationException("Renode LIS2DW12 RESD callback unavailable: " + name);
            }
            return handler;
        }

        private static readonly MethodInfo upstreamAccelerationHandler =
            RequireUpstreamHandler("HandleAccelerationSample");
        private static readonly MethodInfo upstreamAccelerationEndedHandler =
            RequireUpstreamHandler("HandleAccelerationSampleEnded");

        public new void Write(byte[] data)
        {
            if(data != null && data.Length > 0)
            {
                registerPointer = (byte)(data[0] & 0x3F);
                pointerSet = true;
            }

            // The sensor's serial protocol uses bit 7 of the subaddress as
            // the multi-read flag (the firmware sends 0xA8 for OUT_X_L).
            // The upstream model expects a plain register number, so strip
            // protocol flags before forwarding while retaining its native
            // register auto-increment behavior from CTRL2.IF_ADD_INC.
            var upstreamData = (byte[])data.Clone();
            upstreamData[0] &= 0x3F;
            base.Write(upstreamData);
            if(data != null && data.Length > 1 && AutoIncrement())
            {
                registerPointer = (byte)((registerPointer + data.Length - 1) & 0x3F);
            }
            if(data != null && data.Length > 1)
            {
                var writtenRegister = (byte)(data[0] & 0x3F);
                for(var i = 1; i < data.Length; i++)
                {
                    switch(writtenRegister)
                    {
                    case Control1Register: control1 = data[i]; break;
                    case Control3Register: control3 = data[i]; break;
                    case Control4Register: control4 = data[i]; break;
                    case Control6Register: control6 = data[i]; break;
                    case WakeupThresholdRegister: wakeupThreshold = data[i]; break;
                    case WakeupDurationRegister: wakeupDuration = data[i]; break;
                    case Control7Register: control7 = data[i]; break;
                    }
                    if(AutoIncrement()) writtenRegister = (byte)((writtenRegister + 1) & 0x3F);
                }
                CorrectHighPerformance12_5HzRate();
            }
            UpdateWakeupIRQ();
        }

        public new byte[] Read(int count = 1)
        {
            var result = base.Read(count);
            if(!pointerSet) registerPointer = 0;

            if(registerPointer == OutputXLowRegister && result.Length >= 6)
            {
                outputSampleReadCount++;
                lastOutputXRaw = (short)(result[0] | (result[1] << 8));
                lastOutputYRaw = (short)(result[2] | (result[3] << 8));
                lastOutputZRaw = (short)(result[4] | (result[5] << 8));
            }
            for(var i = 0; i < result.Length; i++)
            {
                if(registerPointer == StatusRegister && wakeupPending)
                {
                    result[i] |= WakeupStatusActive;
                }
                if(registerPointer == WakeupSourceRegister)
                {
                    wakeupSourceReadCount++;
                }
                if(registerPointer == WakeupSourceRegister && wakeupPending)
                {
                    result[i] |= wakeupSourceValue;
                    lastWakeupSourceValue = result[i];
                    wakeupPending = false;
                    wakeupEventReadCount++;
                    UpdateWakeupIRQ();
                }
                if(AutoIncrement()) registerPointer = (byte)((registerPointer + 1) & 0x3F);
            }
            return result;
        }

        public new void FinishTransmission() => base.FinishTransmission();

        public void TriggerWakeup()
        {
            wakeupPending = true;
            wakeupSourceValue = WakeupInterruptActive;
            UpdateWakeupIRQ();
        }

        public new void Reset()
        {
            base.Reset();
            wakeupPending = false;
            wakeupIRQAsserted = false;
            wakeupEventReadCount = 0;
            wakeupSourceReadCount = 0;
            lastWakeupSourceValue = 0;
            outputSampleReadCount = 0;
            lastOutputXRaw = 0;
            lastOutputYRaw = 0;
            lastOutputZRaw = 0;
            upstreamIRQAsserted = false;
            control4 = 0;
            control1 = 0;
            control3 = 0;
            control6 = 0;
            control7 = 0;
            wakeupThreshold = 0;
            wakeupDuration = 0;
            wakeupSourceValue = 0;
            thresholdSamples = 0;
            wakeupComparatorSampleCount = 0;
            wakeupGeneratedEventCount = 0;
            filterInitialized = false;
            hasGazeboTimelineSample = false;
            nextWakeupSampleTime = 0m;
            lastAcceleration = new decimal[3];
            highPassAcceleration = new decimal[3];
            registerPointer = 0;
            pointerSet = false;
            UpdateWakeupIRQ();
        }

        private bool AutoIncrement() => (RegistersCollection.Read(Control2Register) & AutoIncrementMask) != 0;

        private void UpdateWakeupIRQ()
        {
            var routeEnabled =
                (control4 & WakeupRouteMask) != 0 &&
                (control7 & InterruptsEnableMask) != 0;
            wakeupIRQAsserted = wakeupPending && routeEnabled;
            Interrupt1.Set(wakeupIRQAsserted || upstreamIRQAsserted);
        }

        private void EvaluateWakeup(decimal xG, decimal yG, decimal zG)
        {
            if((control1 & OdrMask) == 0 || (control7 & InterruptsEnableMask) == 0)
            {
                filterInitialized = false;
                thresholdSamples = 0;
                return;
            }

            var input = new[] { xG, yG, zG };
            wakeupComparatorSampleCount++;
            var odrHz = ConfiguredOutputRateHz();
            var cutoffDivisors = new[] { 2m, 4m, 10m, 20m };
            var cutoffDivisor = cutoffDivisors[(control6 >> BandwidthShift) & 0x03];
            var cutoffHz = odrHz / cutoffDivisor;
            var dt = 1m / odrHz;
            var rc = 1m / (2m * Pi * cutoffHz);
            var alpha = rc / (rc + dt);
            if(!filterInitialized)
            {
                Array.Copy(input, lastAcceleration, input.Length);
                Array.Clear(highPassAcceleration, 0, highPassAcceleration.Length);
                filterInitialized = true;
                return;
            }

            var axes = new byte[] { WakeupXAxis, WakeupYAxis, WakeupZAxis };
            var exceededAxes = (byte)0;
            var threshold = (((wakeupThreshold & WakeupThresholdMask) * FullScaleG()) / 64m);
            for(var axis = 0; axis < input.Length; axis++)
            {
                highPassAcceleration[axis] = alpha *
                    (highPassAcceleration[axis] + input[axis] - lastAcceleration[axis]);
                lastAcceleration[axis] = input[axis];
                if(Math.Abs(highPassAcceleration[axis]) > threshold) exceededAxes |= axes[axis];
            }

            if(exceededAxes == 0)
            {
                thresholdSamples = 0;
                if((control3 & LatchedInterruptMask) == 0)
                {
                    wakeupPending = false;
                    wakeupSourceValue = 0;
                }
                return;
            }

            thresholdSamples++;
            var requiredSamples = (uint)(wakeupDuration & WakeupDurationMask) + 1u;
            if(thresholdSamples >= requiredSamples)
            {
                wakeupPending = true;
                wakeupSourceValue = (byte)(WakeupInterruptActive | exceededAxes);
                thresholdSamples = 0;
                wakeupGeneratedEventCount++;
            }
        }

        private decimal FullScaleG()
        {
            switch((control6 >> FullScaleShift) & 0x03)
            {
            case 0: return 2m;
            case 1: return 4m;
            case 2: return 8m;
            default: return 16m;
            }
        }

        private decimal ConfiguredOutputRateHz()
        {
            var odr = (control1 >> OdrShift) & 0x0F;
            // The silicon's ODR depends on MODE. Renode 1.17 treats code 1 as
            // 1.6 Hz for every mode; in high-performance mode it is 12.5 Hz.
            if(odr == 1 && ((control1 >> ModeShift) & 0x03) == HighPerformanceMode) return 12.5m;
            switch(odr)
            {
            case 1: return 1.6m;
            case 2: return 12.5m;
            case 3: return 25m;
            case 4: return 50m;
            case 5: return 100m;
            case 6: return 200m;
            case 7: return 400m;
            case 8: return 800m;
            case 9: return 1600m;
            default: return 0m;
            }
        }

        private void CorrectHighPerformance12_5HzRate()
        {
            if((control1 & OdrMask) == 0x10 && ((control1 >> ModeShift) & 0x03) == HighPerformanceMode)
            {
                // Upstream exposes an integer private SampleRate setter. Use
                // 13 as the closest scheduler rate to the silicon's 12.5 Hz.
                sampleRateSetter.Invoke(this, new object[] { 13u });
            }
        }

        private static MethodInfo RequireSampleRateSetter()
        {
            var setter = typeof(Sensors.LIS2DW12).GetProperty("SampleRate",
                BindingFlags.Instance | BindingFlags.Public)?.GetSetMethod(true);
            if(setter == null) throw new InvalidOperationException("Renode LIS2DW12 sample-rate setter unavailable");
            return setter;
        }

        private const byte Control2Register = 0x21;
        private const byte Control1Register = 0x20;
        private const byte Control3Register = 0x22;
        private const byte Control4Register = 0x23;
        private const byte Control6Register = 0x25;
        private const byte WakeupThresholdRegister = 0x34;
        private const byte WakeupDurationRegister = 0x35;
        private const byte StatusRegister = 0x27;
        private const byte WakeupSourceRegister = 0x38;
        private const byte Control7Register = 0x3F;
        private const byte OutputXLowRegister = 0x28;
        private const byte AutoIncrementMask = 0x04;
        private const byte WakeupRouteMask = 0x20;
        private const byte InterruptsEnableMask = 0x20;
        private const byte WakeupInterruptActive = 0x08;
        private const byte WakeupStatusActive = 0x40;
        private const byte WakeupThresholdMask = 0x3F;
        private const byte WakeupDurationMask = 0x03;
        private const byte LatchedInterruptMask = 0x10;
        private const byte OdrShift = 4;
        private const byte OdrMask = 0xF0;
        private const byte ModeShift = 2;
        private const byte HighPerformanceMode = 1;
        private const byte BandwidthShift = 6;
        private const byte FullScaleShift = 4;
        private const byte WakeupXAxis = 0x04;
        private const byte WakeupYAxis = 0x02;
        private const byte WakeupZAxis = 0x01;
        private const decimal Pi = 3.1415926535897932384626433833m;

        private byte registerPointer;
        private bool pointerSet;
        private bool wakeupPending;
        private bool wakeupIRQAsserted;
        private bool filterInitialized;
        private byte control1;
        private byte control3;
        private byte control6;
        private byte wakeupThreshold;
        private byte wakeupDuration;
        private byte wakeupSourceValue;
        private uint thresholdSamples;
        private uint wakeupComparatorSampleCount;
        private uint wakeupGeneratedEventCount;
        private decimal[] lastAcceleration = new decimal[3];
        private decimal[] highPassAcceleration = new decimal[3];
        private readonly MethodInfo sampleRateSetter;
        private uint wakeupEventReadCount;
        private uint wakeupSourceReadCount;
        private byte lastWakeupSourceValue;
        private uint outputSampleReadCount;
        private uint gazeboSampleInjectionCount;
        private bool hasGazeboTimelineSample;
        private decimal nextWakeupSampleTime;
        private int lastOutputXRaw;
        private int lastOutputYRaw;
        private int lastOutputZRaw;
        private bool upstreamIRQAsserted;
        private byte control4;
        private byte control7;
    }

}
