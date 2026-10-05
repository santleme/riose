"""Evidence-gated logical RF event and anchor receiver contracts."""

from .telemetry import (
    ANCHOR_LOGICAL_RF_EVENT_ACCEPTED,
    ANTENNA_MODEL_UNVALIDATED,
    LOGICAL_RF_EVENT,
    PHYSICAL_RF_RESULT,
    AnchorLogEntry,
    AnchorReceiver,
    FirmwareTraceError,
    LogicalRfEvent,
    PhysicalRfResult,
    read_firmware_trace,
    logical_events_from_firmware_trace,
)
from .propagation import PropagationEstimate, RFPropagationBackend, SimpleBackend

__all__ = [
    "ANCHOR_LOGICAL_RF_EVENT_ACCEPTED",
    "ANTENNA_MODEL_UNVALIDATED",
    "LOGICAL_RF_EVENT",
    "PHYSICAL_RF_RESULT",
    "AnchorLogEntry",
    "AnchorReceiver",
    "FirmwareTraceError",
    "LogicalRfEvent",
    "PhysicalRfResult",
    "read_firmware_trace",
    "logical_events_from_firmware_trace",
    "PropagationEstimate",
    "RFPropagationBackend",
    "SimpleBackend",
]
