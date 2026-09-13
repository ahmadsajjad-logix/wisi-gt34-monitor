# Trigger Capability Matrix

| Trigger | Current status | Evidence/implementation boundary |
|---|---|---|
| 1. Carrier / demodulation loss | Implemented and validated | GT34 tuner lock telemetry; durable carrier-wide service transitions |
| 2. Video freeze | Not claimed | Requires decoded video/content telemetry; PID presence/bitrate is not equivalent to frozen decoded video |
| 3. Audio freeze / silence | Not claimed | Missing audio PID/track is supported, but silence/freeze requires decoded audio analysis |
| 4. Complete A/V freeze | Not claimed | Requires decoded A/V analysis |
| 5. Black frame / outage | Not claimed | Requires decoded image/frame analysis |
| 6. Color bars / test pattern | Not claimed | Requires decoded image/pattern analysis |
| 7. Channel logo absence | Not claimed | Requires reliable decoded image/ROI analysis |
| 8. Video blur / defocus | Not claimed | Requires decoded image analysis |
| 9. Loudness non-compliance | Not claimed | No verified BS.1770/R128 loudness telemetry in current GT34 feed |
| 10. Missing audio tracks | Implemented and validated | Service-stream inventory and persistent PRESENT/MISSING state |
| 11. One/few channels disappear while carrier remains locked | Implemented and validated | Service-level `last_seen_at` against current tuner sample |
| 12. Commercial over-air timing / SCTE-35/104 | Investigation only | No verified current resource exposing splice-event timing semantics |
| 13. Missing emergency alerts / EAS/CAP | Investigation only | GT34 release history indicates EAS support, but no verified expected-alert/absence monitor interface in current implementation |
| 14. Lip-sync error | Not claimed | PCR alone is insufficient; requires verified PTS/DTS or decoded A/V timing analysis |

## Additional technically verified alarms

The current telemetry also supports monitoring of TEI errors, sync errors, continuity-counter errors, PCR accuracy/repetition/discontinuity counter increments, PCR reference discontinuities, PCR PID changes, playout FIFO reset, and entry into freerunning where these fields are exposed by the GT34 XML resources.

No threshold or regulatory-compliance label should be invented for raw WISI PCR/jitter values unless its engineering unit/threshold is independently verified and approved.
