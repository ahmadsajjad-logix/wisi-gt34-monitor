# Current Status — 2026-09-14

## Confirmed

- 43 authoritative TV-bearing logical carriers.
- PRTG wrapper validation passes with a maximum of 44 / 44 channels.
- Existing production sensors remain in place.
- Python alarm policy and duplicate suppression are implemented.
- TV43 carrier/service history is retained for 15 days.
- SMTP delivery has previously been proven operational.
- PRTG notification triggers are not used.

## Last known genuine service-integrity conditions

- WISI-IRD-01 / M5C5 / Kay 2: Khyber ME HD ES metadata unavailable.
- WISI-IRD-03 / M1C10 / Geo News: Geo ME ES metadata unavailable.

These conditions mean ES metadata was unavailable for those services. They do not by themselves prove decoded video was black/off-air.

## Latest successful checkpoint

V11.3J completed the stable-schema finalization successfully.

Final presentation policy:

- maximum 44 live PRTG channels per sensor;
- all authoritative services remain covered by service-integrity monitoring;
- critical RF/TS measurements remain PRTG channels;
- extra technical/service detail remains in rich sensor status text;
- full observations remain in SQLite for 15 days;
- alarm/email policy remains unchanged;
- commissioning emails are not re-sent.

V11.3J also adds resilience for transient SQLite `database is locked` contention so a persistence collision cannot falsely turn a valid live monitoring result into a transmission alarm.

## Paused follow-up

Verify/finalize exact HTML email rendering against the agreed DOWN/UP screenshots while preserving the current alarm semantics.
