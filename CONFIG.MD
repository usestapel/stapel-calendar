# CONFIG.MD — stapel-calendar

Config registry for **stapel-calendar** (`static-scaffold-and-config.md` §2).
One row per configuration key the module reads, its **source** (`env` = the
process environment / the `STAPEL_CALENDAR` settings dict), what it is for,
whether it is required, and its default.

Every key is read through `calendar_settings`
(`stapel_calendar.conf.AppSettings`, namespace `STAPEL_CALENDAR`). Resolution
order per key: `settings.STAPEL_CALENDAR` dict → a flat Django setting of the
same name → environment variable → the default below. `VISIBILITY` is the one
CTO-facing config axis (capability-config.md §16); the rest are tuning knobs or
extension seams.

## stapel-calendar

| Key | Source | Purpose | Required | Default |
|-----|--------|---------|----------|---------|
| VISIBILITY | env | config axis: `participants` (event visible only to invitees) or `scope` (visible to the whole SCOPE_PROVIDER scope). Unknown value degrades to `participants`. | no | participants |
| DEFAULT_EXPANSION_HORIZON_DAYS | env | days ahead to expand an unbounded recurring series for display. | no | 90 |
| MAX_EXPANSION_OCCURRENCES | env | hard safety cap on occurrences a single expansion may yield. | no | 1000 |
| REMINDER_OFFSETS | env | minutes-before-start the default policy fires `calendar.event.reminder_due`. | no | [10] |
| REMINDER_SCAN_WINDOW_SECONDS | env | granularity (seconds) of the reminder cron scan. | no | 60 |
| REMINDER_POLICY | env | dotted path to a ReminderPolicy (the reminder seam). | no | stapel_calendar.reminders.DefaultReminderPolicy |
| SCOPE_PROVIDER | env | dotted path to a ScopeProvider (resolve/filter the opaque scope_key). | no | stapel_calendar.scope.DefaultScopeProvider |
| PRESETS | env | recurrence presets merged OVER the built-ins (dateutil.rrule kwargs); None removes. | no | {} |
| DEFAULT_SLOT_MINUTES | env | default slot length (minutes) for availability computation. | no | 30 |
