# WISI GT34 Monitor

Private production checkpoint for WISI Tangram GT34 satellite IRD monitoring.

## Capabilities

- multi-chassis WISI GT34 collection;
- SQLite persistence and 15-day TV43 history;
- PRTG EXE/Script Advanced integration;
- authoritative logical-input/service discovery;
- carrier, TS and expected-service integrity monitoring;
- Python-owned alarm/recovery policy and duplicate suppression;
- fixed PRTG presentation schema capped at 44 live channels per sensor.

## Current production scope

The current deployment covers four WISI chassis, eight GT34 modules and 43 authoritative TV-bearing logical carriers. PRTG uses one sensor per logical carrier.

PRTG is the visualization layer. Python remains authoritative for alarm state and email delivery. PRTG notification triggers are intentionally not used.

## Alarm policy

- Immediate alarm; no debounce delay.
- Carrier unlock or TS loss: one DOWN notification covering all affected multiplex services.
- Individual expected-service failure: one DOWN notification for only that service.
- No repeat notification while the same alarm episode remains active.
- Immediate recovery notification.
- Recovery includes total downtime.

## Agreed email presentation

Production notifications use the **Transmission Alert** format with:

- date/time stamp in PKT;
- channel name;
- satellite name;
- frequency band;
- `❌ DOWN` or `✅ UP`;
- total downtime for recovery;
- PEMRA logo from `PEMRA_Logo.png`;
- automated transmission-monitoring footer.

## PRTG stable schema

Latest installer checkpoint:

`install_tv43_prtg_stable_v113j.ps1`

V11.3J is the successful stable-schema checkpoint. It preserves the proven Phase 4 wrapper contract and adds SQLite lock resilience:

```text
wisi_gt34_carrier.bat --tv43 --host <host> --module <module> --channel <channel>
```

The maximum live PRTG return remains **44 channels**. Additional technical detail remains in sensor status text and SQLite.

## Security

Operational credentials and runtime data are deliberately excluded from source control.

The repository version of `email_notifier.py` reads SMTP configuration from environment variables. See `.env.example`.

Do not commit PRTG passhashes, SMTP passwords, runtime databases, logs or local secret files.

## Status

This repository is a private checkpoint taken after the successful V11.3J run. Monitoring, history, PRTG integration and the alarm engine are operational. The final presentation policy is frozen at a maximum of 44 live PRTG channels per sensor, with overflow detail retained in rich status text and 15-day SQLite history. Exact final verification of the HTML email renderer against the agreed production screenshots remains the next controlled follow-up.

See `docs/CURRENT_STATUS.md`.


## V11.3K — email presentation checkpoint

Presentation-only update. Alarm timing, duplicate suppression, recovery
semantics, PRTG schema, 44-channel cap, SQLite retention and monitoring logic
are unchanged.

`tv43_alarm_policy_final.py` renders the agreed HTML `Transmission Alert`
format with PKT timestamp, channel name, Paksat MM1, C-band, DOWN/UP state,
recovery downtime, inline PEMRA logo and the automated-notification footer.

`send_tv43_format_test.py` sends the exact same production renderer with
synthetic data. It does not read, create, rearm, clear, or modify alarm
episodes, so commissioning emails are not re-sent.


## V11.3L — screenshot-matched email presentation

Email presentation was aligned to the approved alarm/recovery screenshots:

- PEMRA logo is emoji-sized and inline at the beginning of the italic footer;
- footer has no separate centered logo or horizontal separator;
- alarm body ends at `Channel status: ❌ DOWN`;
- recovery body adds `Total downtime: HH:MM:SS` after `Channel status: ✅ UP`;
- no extra alarm-start or recovery-clear rows are displayed in the recovery email.

Monitoring logic, immediate notification semantics, duplicate suppression,
service-integrity logic, PRTG schema, 44-channel cap and SQLite retention are
unchanged.
