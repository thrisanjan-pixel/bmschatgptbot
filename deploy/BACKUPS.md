# Reports and recovery

Each saved alert reports successful check-gap averages and maximums from samples collected after alert creation within seven days. Existing historical totals cannot reconstruct old gaps. Queue delay is time from creating an outbox message to successful Telegram delivery; it does not measure undetected source changes or the time a person sees the message.

Hourly versioned JSON backups retain up to 24 copies beside the SQLite database in backups/. These protect against some accidental changes, not volume deletion. Activity offers a current download; keep that file off the hosting volume. No external backup storage is connected.

Backups include alerts, source sessions, cached layouts, row and initial baseline state, and check measurements. They exclude environment variables, Telegram credentials, dashboard login sessions, meta settings, outgoing message queues and image history. Restore keeps existing IDs untouched, imports missing alerts paused, and preserves existing source records. Fresh source checks are required before resuming; a cached layout is not current availability. Imported alerts are subject to their original expiry and completion status. Queued messages are excluded to avoid replay.

To migrate: download a copy, configure hosting and Telegram credentials privately on the destination, import using Activity, inspect restored filters, allow source refresh, and resume alerts explicitly. Retain the old copy until recovery is verified. Do not run two enabled copies that send duplicate alerts.
