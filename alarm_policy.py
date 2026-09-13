from __future__ import annotations

# ---------------------------------------------------------------------------
# WISI GT34 alarm policy
# ---------------------------------------------------------------------------
#
# These are the tuner inputs expected to carry live services in the currently
# validated installation baseline.
#
# IMPORTANT:
# - input_id is the canonical WISI 0..7 input identity used by the database.
# - An input omitted here is treated as intentionally not monitored for
#   lock/TS-loss alarms.
# - If a currently omitted input is supposed to carry a service, add its
#   (module_number, input_id) tuple before production alerting is enabled.
#
# Baseline derived from the validated live snapshot of 2026-09-11:
#   Module 1 active: inputs 0, 2, 4, 5, 6, 7
#   Module 5 active: inputs 0, 4, 5, 6, 7
# ---------------------------------------------------------------------------

EXPECTED_ACTIVE_INPUTS: set[tuple[int, int]] = {
    (1, 0),
    (1, 2),
    (1, 4),
    (1, 5),
    (1, 6),
    (1, 7),
    (5, 0),
    (5, 4),
    (5, 5),
    (5, 6),
    (5, 7),
}

# RF/SNR/BER policy thresholds remain disabled until an approved operational
# threshold is selected. None means "do not alarm on this metric".
RF_LEVEL_MIN_DBM: float | None = None
SNR_MIN_DB: float | None = None
BER_MAX: float | None = None
