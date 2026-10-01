# Architecture

## Collection
Python polls WISI GT34/Tangram resources, parses technical/service state and writes SQLite observations.

## Alarm engine
Python evaluates carrier/TS/service integrity, persists episodes, suppresses duplicate notifications and emits recovery when an episode clears.

## PRTG
One PRTG sensor represents one authoritative logical TV-bearing carrier. The numeric schema is frozen and capped at 44 channels. Rich overflow telemetry stays in status text and SQLite history.

## Retention
TV43 carrier and service history is retained for 30 days.

## Security
Secrets, PRTG passhashes, production DBs and logs are operational state and are excluded from Git.
