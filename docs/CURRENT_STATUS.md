# Current Status - 2026-10-01

## Production checkpoint

- Version: `11.6-wisi-route-mcpc-notification-correction`.
- Git commit: `caa54151bfee4d19745838b524dc4ef12940c8d0`.
- V11.6 production activation: PASS.
- Alarm Policy Scheduled Task: Running after deployment.
- Central Collector remained running during deployment.

## Authoritative monitoring scope

- 43 authoritative administrative TV-bearing carriers.
- Adaptive monitoring authority between WISI Tangram GT34 and Wellav CMP201.
- V11.5 commissioning snapshot: 33 WISI, 10 Wellav, 0 ambiguous, 0 unavailable.
- The 33/10 split is not permanently fixed; future WISI-to-Wellav and Wellav-to-WISI migration is supported.
- Administrative carrier identity remains stable when the physical monitoring source changes.
- Source-route history and source-specific cursors are retained.

## Alarm policy

- Standard transport/service DOWN: 15 seconds.
- Sustained carrier unlock: 20 seconds.
- WISI NULL-only PID 8191 payload: 20 seconds.
- Recovery: 10 seconds.
- Observation gap greater than 8 seconds resets persistence continuity.
- Missing or stale telemetry alone is monitoring uncertainty, not transmission DOWN or UP evidence.

## V11.6 corrections

- Genuine WISI RF unlock is no longer incorrectly converted into `expected_path_down` when dynamic mapping temporarily disappears.
- The established physical WISI route is checked and its tuner/transport history is reconstructed before path absence is inferred.
- MCPC carrier/transport alerts use grouped multiplex-service notification formatting.
- MCPC individual-service alerts list only the affected services.
- Parent MCPC recovery no longer falsely reports independently unavailable services as UP.
- SCPC notification formatting remains unchanged.

## Delivery and presentation

- Python/SQLite remains authoritative for transmission alarm state, persistence, duplicate suppression and recovery.
- PRTG remains the visualization layer.
- PRTG notification triggers are not used as the authoritative transmission-alert engine.
- Alarm/recovery email uses the durable SQLite delivery outbox.
- Production SMTP and Wellav credentials remain outside source control.

## Latest production validation

- Historical 11566 MHz WISI unlock reconstruction: PASS.
- False `expected_path_down` prevention: PASS.
- MCPC carrier-DOWN format: PASS.
- MCPC service-DOWN format: PASS.
- Partial MCPC recovery accuracy: PASS.
- SCPC format preservation: PASS.
- Final isolated dry run: 43/43 source routes and 43/43 source cursors.
- Dry-run new notifications: 0.
- Test emails sent during activation: none.

## Recent forensic findings

- Khyber ME HD / 4078 MHz H: the reviewed DOWN event was genuine service-level evidence; no recovery had been recorded at the time of the audit.
- 11566 MHz H: genuine RF carrier unlock; V11.6 corrected the prior WISI route-classification behavior.
- Public News: authoritative observations showed only short interruptions below the configured persistence thresholds, so no production alert qualified.
- 3797 MHz H / KTN multiplex: WISI PID evidence showed NULL-only transport payload while carrier lock remained present, supporting the multiplex-level outage.
- 11552 MHz H: both DOWN and recovery notification records existed; V11.6 now prevents parent recovery from falsely implying recovery of independently unavailable services.

These incident findings describe the reviewed historical windows and are not statements of current live channel status.

## Operational baseline

Production changes must remain incremental and evidence-driven. Use read-only diagnostics first, preserve validated monitoring behavior, back up production state before modification, and avoid redesigning the established identity, persistence, database, PRTG or notification architecture without explicit approval.
