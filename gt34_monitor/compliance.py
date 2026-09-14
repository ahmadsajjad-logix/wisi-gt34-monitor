from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from .collector import GT34Poller
from .fleet import FleetIRD, ServiceBaseline, parse_input_key
from .models import ConfiguredInput, ReceiverSample


@dataclass(frozen=True, slots=True)
class ComplianceFinding:
    severity: str
    code: str
    ird_name: str
    host: str
    module: int | None = None
    channel: int | None = None
    sid: int | None = None
    expected: str | int | float | None = None
    actual: str | int | float | None = None
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class IRDComplianceResult:
    ird_name: str
    host: str

    baseline_input_count: int = 0
    expected_service_count: int = 0

    live_gt34_input_count: int = 0
    live_active_input_count: int = 0
    checked_baseline_inputs: int = 0

    missing_service_count: int = 0
    unexpected_service_count: int = 0
    name_mismatch_count: int = 0
    explicit_service_failure_count: int = 0
    carrier_or_ts_error_count: int = 0

    # Collection state is deliberately separate from compliance state.
    collection_ok: bool = True
    collection_attempts: int = 1
    collection_error: str | None = None

    findings: list[ComplianceFinding] = field(default_factory=list)

    @property
    def compliance_error_count(self) -> int:
        return sum(
            1
            for item in self.findings
            if item.severity == "ERROR"
            and item.code != "IRD_COLLECTION_FAILED"
        )

    @property
    def error_count(self) -> int:
        return self.compliance_error_count + (0 if self.collection_ok else 1)

    @property
    def warning_count(self) -> int:
        return sum(1 for item in self.findings if item.severity == "WARNING")

    @property
    def info_count(self) -> int:
        return sum(1 for item in self.findings if item.severity == "INFO")

    @property
    def compliance_status(self) -> str:
        if not self.collection_ok:
            return "UNKNOWN"
        if self.compliance_error_count:
            return "ERROR"
        if self.warning_count:
            return "WARNING"
        return "OK"

    @property
    def status(self) -> str:
        if not self.collection_ok:
            return "COLLECTION_ERROR"
        return self.compliance_status

    def add(self, finding: ComplianceFinding) -> None:
        self.findings.append(finding)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ird_name": self.ird_name,
            "host": self.host,
            "status": self.status,
            "compliance_status": self.compliance_status,
            "collection_ok": self.collection_ok,
            "collection_attempts": self.collection_attempts,
            "collection_error": self.collection_error,
            "baseline_input_count": self.baseline_input_count,
            "expected_service_count": self.expected_service_count,
            "live_gt34_input_count": self.live_gt34_input_count,
            "live_active_input_count": self.live_active_input_count,
            "checked_baseline_inputs": self.checked_baseline_inputs,
            "missing_service_count": self.missing_service_count,
            "unexpected_service_count": self.unexpected_service_count,
            "name_mismatch_count": self.name_mismatch_count,
            "explicit_service_failure_count": self.explicit_service_failure_count,
            "carrier_or_ts_error_count": self.carrier_or_ts_error_count,
            "compliance_error_count": self.compliance_error_count,
            "error_count": self.error_count,
            "warning_count": self.warning_count,
            "info_count": self.info_count,
            "findings": [item.to_dict() for item in self.findings],
        }


@dataclass(slots=True)
class FleetComplianceResult:
    irds: list[IRDComplianceResult]

    @property
    def collection_error_ird_count(self) -> int:
        return sum(1 for ird in self.irds if not ird.collection_ok)

    @property
    def compliance_error_ird_count(self) -> int:
        return sum(
            1
            for ird in self.irds
            if ird.collection_ok and ird.compliance_status == "ERROR"
        )

    @property
    def warning_ird_count(self) -> int:
        return sum(
            1
            for ird in self.irds
            if ird.collection_ok and ird.compliance_status == "WARNING"
        )

    @property
    def status(self) -> str:
        if self.collection_error_ird_count:
            return "COLLECTION_ERROR"
        if self.compliance_error_ird_count:
            return "ERROR"
        if self.warning_ird_count:
            return "WARNING"
        return "OK"

    # Backward-compatible alias used by older rendering code.
    @property
    def error_ird_count(self) -> int:
        return self.collection_error_ird_count + self.compliance_error_ird_count

    @property
    def baseline_input_count(self) -> int:
        return sum(ird.baseline_input_count for ird in self.irds)

    @property
    def expected_service_count(self) -> int:
        return sum(ird.expected_service_count for ird in self.irds)

    @property
    def missing_service_count(self) -> int:
        return sum(ird.missing_service_count for ird in self.irds)

    @property
    def unexpected_service_count(self) -> int:
        return sum(ird.unexpected_service_count for ird in self.irds)

    @property
    def name_mismatch_count(self) -> int:
        return sum(ird.name_mismatch_count for ird in self.irds)

    @property
    def carrier_or_ts_error_count(self) -> int:
        return sum(ird.carrier_or_ts_error_count for ird in self.irds)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "ird_count": len(self.irds),
            "collection_error_ird_count": self.collection_error_ird_count,
            "compliance_error_ird_count": self.compliance_error_ird_count,
            "error_ird_count": self.error_ird_count,
            "warning_ird_count": self.warning_ird_count,
            "baseline_input_count": self.baseline_input_count,
            "expected_service_count": self.expected_service_count,
            "missing_service_count": self.missing_service_count,
            "unexpected_service_count": self.unexpected_service_count,
            "name_mismatch_count": self.name_mismatch_count,
            "carrier_or_ts_error_count": self.carrier_or_ts_error_count,
            "irds": [ird.to_dict() for ird in self.irds],
        }


def _finding(
    *,
    severity: str,
    code: str,
    ird: FleetIRD,
    module: int | None = None,
    channel: int | None = None,
    sid: int | None = None,
    expected: str | int | float | None = None,
    actual: str | int | float | None = None,
    message: str,
) -> ComplianceFinding:
    return ComplianceFinding(
        severity=severity,
        code=code,
        ird_name=ird.name,
        host=ird.host,
        module=module,
        channel=channel,
        sid=sid,
        expected=expected,
        actual=actual,
        message=message,
    )


def collection_failure_result(
    *,
    ird: FleetIRD,
    baseline: ServiceBaseline | None,
    attempts: int,
    message: str,
) -> IRDComplianceResult:
    """
    Build a collection-failure result while preserving the approved baseline
    counts. This prevents fleet totals from collapsing merely because one IRD
    could not be polled during the current cycle.
    """
    result = IRDComplianceResult(
        ird_name=ird.name,
        host=ird.host,
        baseline_input_count=0 if baseline is None else baseline.input_count,
        expected_service_count=0 if baseline is None else baseline.service_count,
        collection_ok=False,
        collection_attempts=attempts,
        collection_error=message,
    )
    result.add(
        _finding(
            severity="ERROR",
            code="IRD_COLLECTION_FAILED",
            ird=ird,
            expected="successful SNMP collection",
            actual=message,
            message=message,
        )
    )
    return result


def _evaluate_expected_services(
    *,
    result: IRDComplianceResult,
    ird: FleetIRD,
    sample: ReceiverSample,
    expected_services: dict[int, str],
) -> None:
    live = sample.live_services_by_sid

    for sid, expected_name in sorted(expected_services.items()):
        service = live.get(sid)

        if service is None:
            result.missing_service_count += 1
            result.add(
                _finding(
                    severity="ERROR",
                    code="EXPECTED_SERVICE_MISSING",
                    ird=ird,
                    module=sample.module,
                    channel=sample.channel,
                    sid=sid,
                    expected=expected_name,
                    actual=None,
                    message=(
                        f"Expected service {expected_name!r} SID {sid} is absent "
                        f"from m{sample.module}/c{sample.channel}"
                    ),
                )
            )
            continue

        if service.name.casefold().strip() != expected_name.casefold().strip():
            result.name_mismatch_count += 1
            result.add(
                _finding(
                    severity="WARNING",
                    code="SERVICE_NAME_CHANGED",
                    ird=ird,
                    module=sample.module,
                    channel=sample.channel,
                    sid=sid,
                    expected=expected_name,
                    actual=service.name,
                    message=(
                        f"SID {sid} name changed from {expected_name!r} "
                        f"to {service.name!r}"
                    ),
                )
            )

        if service.explicit_failure:
            result.explicit_service_failure_count += 1
            result.add(
                _finding(
                    severity="ERROR",
                    code="EXPECTED_SERVICE_NOT_RUNNING",
                    ird=ird,
                    module=sample.module,
                    channel=sample.channel,
                    sid=sid,
                    expected="operational/present",
                    actual=service.status_label,
                    message=(
                        f"Expected service {expected_name!r} SID {sid} reports "
                        f"explicit non-running status {service.status_label!r}"
                    ),
                )
            )
        elif int(service.elementary_stream_count or 0) <= 0:
            result.explicit_service_failure_count += 1
            result.add(
                _finding(
                    severity="ERROR",
                    code="EXPECTED_SERVICE_ES_PID_UNAVAILABLE",
                    ird=ird,
                    module=sample.module,
                    channel=sample.channel,
                    sid=sid,
                    expected="at least one elementary stream/PID",
                    actual=0,
                    message=(
                        f"Expected service {expected_name!r} SID {sid} is present "
                        "but WISI exposes no elementary stream/PID rows"
                    ),
                )
            )
        elif not service.running:
            # UNKNOWN, STATUS-7 and other vendor-specific values prove that the
            # service exists but do not, by themselves, prove service failure.
            result.add(
                _finding(
                    severity="INFO",
                    code="SERVICE_STATUS_UNVERIFIED",
                    ird=ird,
                    module=sample.module,
                    channel=sample.channel,
                    sid=sid,
                    expected="running or vendor-verified state",
                    actual=service.status_label,
                    message=(
                        f"Service {service.name!r} SID {sid} is present but its "
                        f"WISI status is {service.status_label!r}; no failure "
                        "is inferred from this value"
                    ),
                )
            )

    for service in sorted(sample.services, key=lambda item: item.sid):
        if service.sid in expected_services:
            continue

        result.unexpected_service_count += 1
        result.add(
            _finding(
                severity="WARNING",
                code="UNEXPECTED_SERVICE",
                ird=ird,
                module=sample.module,
                channel=sample.channel,
                sid=service.sid,
                expected=None,
                actual=service.name,
                message=(
                    f"Unexpected live service {service.name!r} SID {service.sid} "
                    f"appeared on m{sample.module}/c{sample.channel}"
                ),
            )
        )


def evaluate_ird(
    *,
    ird: FleetIRD,
    baseline: ServiceBaseline,
    poller: GT34Poller,
) -> IRDComplianceResult:
    result = IRDComplianceResult(
        ird_name=ird.name,
        host=ird.host,
        baseline_input_count=baseline.input_count,
        expected_service_count=baseline.service_count,
    )

    live_inputs = poller.discover_gt34_inputs()
    live_by_key: dict[str, ConfiguredInput] = {
        f"{row.module}.{row.channel}": row
        for row in live_inputs
    }
    result.live_gt34_input_count = len(live_inputs)

    # Baseline inputs are the compliance-controlled carrier set.
    for input_key, expected_services in sorted(baseline.expected_mux.items()):
        module, channel = parse_input_key(input_key)
        input_row = live_by_key.get(input_key)

        if input_row is None:
            result.carrier_or_ts_error_count += 1
            result.missing_service_count += len(expected_services)
            result.add(
                _finding(
                    severity="ERROR",
                    code="EXPECTED_INPUT_MISSING",
                    ird=ird,
                    module=module,
                    channel=channel,
                    expected=(
                        f"configured GT34 input with {len(expected_services)} services"
                    ),
                    actual=None,
                    message=(
                        f"Baseline input m{module}/c{channel} is not present in "
                        "the current GT34 input catalogue"
                    ),
                )
            )
            continue

        sample = poller.poll_input(input_row)
        result.checked_baseline_inputs += 1

        if sample.services:
            result.live_active_input_count += 1

        carrier_error = not sample.effective_locked
        ts_error = sample.ts_present is not True
        input_transport_failed = carrier_error or ts_error

        # Count affected baseline inputs, not individual fault conditions.
        # A single input that is both unlocked and TS-absent contributes only
        # one to carrier_or_ts_error_count. The detailed carrier/TS findings
        # remain separate so the root cause is still explicit.
        if input_transport_failed:
            result.carrier_or_ts_error_count += 1

        if carrier_error:
            result.add(
                _finding(
                    severity="ERROR",
                    code="CARRIER_NOT_OPERATIONAL",
                    ird=ird,
                    module=module,
                    channel=channel,
                    expected="effective lock",
                    actual="unlocked/not-operational",
                    message=(
                        f"Baseline input m{module}/c{channel} "
                        "is not operationally locked"
                    ),
                )
            )

        if ts_error:
            result.add(
                _finding(
                    severity="ERROR",
                    code="TS_ABSENT",
                    ird=ird,
                    module=module,
                    channel=channel,
                    expected="transport stream present",
                    actual=(
                        "unknown" if sample.ts_present is None else "absent"
                    ),
                    message=(
                        f"Baseline input m{module}/c{channel} "
                        "has no confirmed transport stream"
                    ),
                )
            )

        if input_transport_failed:
            # Root-cause-aware classification: expected services on an input
            # with no operational carrier/TS are unavailable because of the
            # transport failure, not independent service-level failures.
            # Preserve visibility as INFO findings without inflating
            # missing_service_count.
            live = sample.live_services_by_sid
            for sid, expected_name in sorted(expected_services.items()):
                if sid in live:
                    continue

                result.add(
                    _finding(
                        severity="INFO",
                        code="EXPECTED_SERVICE_UNAVAILABLE_DUE_TO_CARRIER_TS",
                        ird=ird,
                        module=sample.module,
                        channel=sample.channel,
                        sid=sid,
                        expected=expected_name,
                        actual=None,
                        message=(
                            f"Expected service {expected_name!r} SID {sid} is "
                            f"unavailable on m{sample.module}/c{sample.channel} "
                            "because the carrier/transport stream is not operational"
                        ),
                    )
                )
        else:
            _evaluate_expected_services(
                result=result,
                ird=ird,
                sample=sample,
                expected_services=expected_services,
            )

    # Active service-bearing inputs outside the baseline are warnings. Spare
    # configured-but-unlocked inputs are deliberately ignored.
    for input_key, input_row in sorted(live_by_key.items()):
        if input_key in baseline.expected_mux:
            continue

        sample = poller.poll_input(input_row)

        if not sample.services:
            continue

        result.live_active_input_count += 1
        result.add(
            _finding(
                severity="WARNING",
                code="UNBASELINED_ACTIVE_INPUT",
                ird=ird,
                module=sample.module,
                channel=sample.channel,
                expected="input absent from approved baseline",
                actual=f"{len(sample.services)} live services",
                message=(
                    f"Active input m{sample.module}/c{sample.channel} "
                    f"({sample.display_name}) carries {len(sample.services)} "
                    "services but is not present in the approved baseline"
                ),
            )
        )

    return result
