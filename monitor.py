from __future__ import annotations

import argparse
import logging
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from alarm_persistence import evaluate_and_persist
from collector import collect_once
from config import LOG_PATH, POLL_INTERVAL_SECONDS
from channel_monitor import evaluate_channel_states
from email_notifier import send_channel_transition_notifications
from retention import RETENTION_DAYS, cleanup_retention


RETENTION_INTERVAL_SECONDS = 24 * 60 * 60
LOG_MAX_BYTES = 5 * 1024 * 1024
LOG_BACKUP_COUNT = 5


def configure_logging() -> logging.Logger:
    log_path = Path(LOG_PATH)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger("wisi_monitor")
    logger.setLevel(logging.INFO)
    logger.propagate = False

    if logger.handlers:
        return logger

    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)s | %(message)s"
    )

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(formatter)

    file_handler = RotatingFileHandler(
        log_path,
        maxBytes=LOG_MAX_BYTES,
        backupCount=LOG_BACKUP_COUNT,
        encoding="utf-8",
    )
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(formatter)

    logger.addHandler(console_handler)
    logger.addHandler(file_handler)

    return logger


def summarize_collection(
    result: dict[str, Any],
) -> str:
    module_parts: list[str] = []
    modules = result.get("modules", {})

    for module_number in sorted(modules):
        info = modules[module_number]

        if info.get("ok"):
            module_parts.append(
                f"M{module_number}=OK"
                f"({info.get('elapsed_seconds', '?')}s)"
            )
        else:
            module_parts.append(
                f"M{module_number}=FAIL"
            )

    module_text = ", ".join(module_parts)

    return (
        f"poll_run_id={result.get('poll_run_id')} | "
        f"success={result.get('success')} | "
        f"duration={result.get('duration_seconds')}s | "
        f"modules={result.get('modules_succeeded')}/"
        f"{result.get('modules_attempted')} | "
        f"{module_text}"
    )


def run_retention(
    logger: logging.Logger,
) -> bool:
    try:
        result = cleanup_retention(
            retention_days=RETENTION_DAYS,
            apply=True,
        )

        logger.info(
            "Retention complete | "
            "retention_days=%s | "
            "expired=%s | "
            "deleted=%s | "
            "cutoff=%s",
            result["retention_days"],
            result["total_expired"],
            result["total_deleted"],
            result["cutoff"],
        )

        return True

    except Exception:
        logger.exception(
            "Retention failed; monitoring will continue."
        )
        return False


def run_alarm_processing(
    logger: logging.Logger,
    *,
    cycle_number: int,
) -> bool:
    try:
        evaluation, persistence = evaluate_and_persist()

        logger.info(
            "Alarm cycle %s complete | "
            "evaluated=%s | "
            "event_inserted=%s | "
            "state_opened=%s | "
            "state_refreshed=%s | "
            "state_recovered=%s | "
            "duplicates_suppressed=%s",
            cycle_number,
            persistence.evaluated_alarms,
            persistence.event_alarms_inserted,
            persistence.state_alarms_opened,
            persistence.state_alarms_refreshed,
            persistence.state_alarms_recovered,
            persistence.duplicate_events_suppressed,
        )

        critical_count = sum(
            1
            for alarm in evaluation["alarms"]
            if alarm.severity == "critical"
        )
        warning_count = sum(
            1
            for alarm in evaluation["alarms"]
            if alarm.severity == "warning"
        )

        if critical_count or warning_count:
            logger.warning(
                "Current alarm snapshot | "
                "cycle=%s | critical=%s | warning=%s | total=%s",
                cycle_number,
                critical_count,
                warning_count,
                len(evaluation["alarms"]),
            )

        return True


    except Exception:
        logger.exception(
            "Alarm processing failed after collection cycle %s; "
            "collection monitoring will continue.",
            cycle_number,
        )
        return False


def run_channel_processing(
    logger: logging.Logger,
    *,
    cycle_number: int,
) -> bool:
    try:
        result = evaluate_channel_states()
        logger.info(
            "Channel cycle %s | services=%s | availability_transitions=%s | "
            "audio_tracks=%s | audio_transitions=%s | inventory_added=%s | "
            "audio_inventory_added=%s",
            cycle_number,
            result.services_evaluated,
            result.availability_transitions,
            result.audio_tracks_evaluated,
            result.audio_transitions,
            result.inventory_added,
            result.audio_inventory_added,
        )

        # Always check for pending channel transition emails after every
        # successful channel-state evaluation. This guarantees that a
        # transition persisted during an earlier cycle is retried if SMTP
        # failed, even when the current cycle has no new state transition.
        notification = send_channel_transition_notifications()
        if notification.candidates or notification.sent:
            logger.info(
                "Channel email cycle %s | candidates=%s | sent=%s",
                cycle_number,
                notification.candidates,
                notification.sent,
            )
        return True
    except Exception:
        logger.exception(
            "Channel-state processing failed after collection cycle %s; "
            "collection/alarm monitoring will continue.",
            cycle_number,
        )
        return False


def run_monitor(
    *,
    interval_seconds: float = POLL_INTERVAL_SECONDS,
    max_cycles: int | None = None,
    retention_enabled: bool = True,
) -> int:
    logger = configure_logging()

    if interval_seconds <= 0:
        logger.error(
            "Polling interval must be greater than zero."
        )
        return 1

    if max_cycles is not None and max_cycles <= 0:
        logger.error(
            "max_cycles must be greater than zero when supplied."
        )
        return 1

    logger.info("=" * 88)
    logger.info("WISI GT34 MONITOR STARTED")
    logger.info(
        "poll_interval=%ss | retention=%s | "
        "retention_days=%s | alarms=enabled | email=enabled | "
        "max_cycles=%s",
        interval_seconds,
        "enabled" if retention_enabled else "disabled",
        RETENTION_DAYS,
        max_cycles if max_cycles is not None else "continuous",
    )
    logger.info("=" * 88)

    cycle_number = 0

    if retention_enabled:
        run_retention(logger)

    last_retention_monotonic = time.monotonic()

    try:
        while True:
            cycle_number += 1
            cycle_started_monotonic = time.monotonic()

            logger.info(
                "Collection cycle %s starting.",
                cycle_number,
            )

            try:
                result = collect_once()

                if result.get("success"):
                    logger.info(
                        "Collection cycle %s complete | %s",
                        cycle_number,
                        summarize_collection(result),
                    )

                    run_alarm_processing(
                        logger,
                        cycle_number=cycle_number,
                    )

                    run_channel_processing(
                        logger,
                        cycle_number=cycle_number,
                    )

                else:
                    logger.error(
                        "Collection cycle %s failed | %s | errors=%s",
                        cycle_number,
                        summarize_collection(result),
                        result.get("errors", []),
                    )
                    logger.warning(
                        "Alarm processing skipped for failed "
                        "collection cycle %s.",
                        cycle_number,
                    )

            except Exception:
                logger.exception(
                    "Unhandled exception in collection cycle %s; "
                    "monitoring will continue.",
                    cycle_number,
                )

            if max_cycles is not None and cycle_number >= max_cycles:
                logger.info(
                    "Requested cycle limit reached: %s",
                    max_cycles,
                )
                break

            now_monotonic = time.monotonic()

            if (
                retention_enabled
                and (
                    now_monotonic
                    - last_retention_monotonic
                    >= RETENTION_INTERVAL_SECONDS
                )
            ):
                run_retention(logger)
                last_retention_monotonic = time.monotonic()
                now_monotonic = last_retention_monotonic

            cycle_elapsed = (
                time.monotonic()
                - cycle_started_monotonic
            )

            sleep_seconds = (
                interval_seconds
                - cycle_elapsed
            )

            if sleep_seconds > 0:
                logger.info(
                    "Next collection in %.3f seconds.",
                    sleep_seconds,
                )
                time.sleep(sleep_seconds)

            else:
                logger.warning(
                    "Polling interval overrun | "
                    "cycle=%s | elapsed=%.3fs | target=%.3fs | "
                    "next cycle will start immediately; "
                    "no overlapping collector is started.",
                    cycle_number,
                    cycle_elapsed,
                    interval_seconds,
                )

    except KeyboardInterrupt:
        logger.info(
            "Ctrl+C received. Shutting down monitor cleanly."
        )

    finally:
        logger.info("=" * 88)
        logger.info(
            "WISI GT34 MONITOR STOPPED | cycles=%s",
            cycle_number,
        )
        logger.info("=" * 88)

    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run continuous WISI GT34 collection, alarm processing, "
            "automatic transmission-alert email notification, "
            "non-overlapping polling, and database retention."
        )
    )

    parser.add_argument(
        "--interval",
        type=float,
        default=float(POLL_INTERVAL_SECONDS),
        help=(
            "Target polling interval in seconds "
            f"(default: {POLL_INTERVAL_SECONDS})."
        ),
    )

    parser.add_argument(
        "--cycles",
        type=int,
        default=None,
        help=(
            "Stop after this many collection cycles. "
            "Omit for continuous monitoring."
        ),
    )

    parser.add_argument(
        "--no-retention",
        action="store_true",
        help=(
            "Disable automatic retention cleanup for this process."
        ),
    )

    return parser.parse_args()


def main() -> int:
    args = parse_args()

    return run_monitor(
        interval_seconds=args.interval,
        max_cycles=args.cycles,
        retention_enabled=not args.no_retention,
    )


if __name__ == "__main__":
    raise SystemExit(main())
