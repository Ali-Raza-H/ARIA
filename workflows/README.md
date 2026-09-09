# ARIA autonomous workflows

ARIA loads `*.yaml` and `*.yml` files from the configured
`scheduler.workflow_directory` when the process starts. The scheduler is
**in-process**: jobs persist in SQLite and survive restarts, but they execute
only while ARIA is running.

## Format

```yaml
workflows:
  - name: weekday_followup
    cron: "0 17 * * 1-5"
    category: analysis
    action:
      type: deadline_check
    misfire_policy: skip
    enabled: true
```

`cron` uses five local-time fields: `minute hour day-of-month month weekday`.
Weekday follows standard cron numbering (`0`/`7` Sunday through `6` Saturday).
Supported field forms are `*`, `*/step`, comma-separated values, ranges, and
single integers.

`misfire_policy` is either:

- `skip`: do not replay a missed run after restart.
- `run_once`: replay one missed run when ARIA starts.

Every attempted job is recorded in the scheduler database, including blocked
jobs and failures. Use `/scheduler` to inspect the active database and
allowlist status.

## Autonomy categories

Read-only `analysis` workflows (briefings and monitoring) run when
`scheduler.analysis_enabled: true` (the default). Consequential workflows run
only when both `autonomy.enabled: true` and their category appears in
`autonomy.allowed_categories`.

- `analysis`: briefings, deadline/goal/calendar/routine checks, profile checks.
- `lifeos_writes`: LifeOS writes, additionally restricted by the explicit
  `autonomy.lifeos_write_operations` list.
- `notifications`: Dunst-compatible `notify-send` or configured D-Bus notices,
  with optional TTS.
- `computer_control`: ARIA desktop/browser tool calls from a workflow action.
- `timers`: timer creation and timer controls.

The default scheduler jobs provide morning, afternoon, and end-of-day briefings
plus deadline, goal, calendar, and private profile checks. These read-only
analysis jobs run by default when `scheduler.analysis_enabled` is true; they do
not require the autonomy write allowlist.

## Built-in action forms

```yaml
# Briefing
- name: morning
  cron: "0 8 * * *"
  category: analysis
  action: {type: briefing, period: morning}

# Monitoring
- name: deadline_risk
  cron: "*/30 * * * *"
  category: analysis
  action: {type: deadline_check}

# Explicit LifeOS write
- name: followup_task
  cron: "0 17 * * 1-5"
  category: lifeos_writes
  action:
    type: lifeos_write
    operation: create_task
    arguments:
      title: Review follow-up suggestions

# Notification
- name: focus_start
  cron: "0 9 * * 1-5"
  category: notifications
  action:
    type: notice
    body: Start the most important task.
    urgency: normal

# Timer
- name: break_timer
  cron: "30 10 * * 1-5"
  category: timers
  action:
    type: create
    name: Morning break
    kind: reminder
    duration_seconds: 900
    persistent: true
```

`vision.periodic_screen_enabled` is separately required for periodic screen
context. When enabled, ARIA adds a scheduled `screen_check` analysis job using
`vision.periodic_screen_cron`; screenshots are temporary and are not retained
by default. Screen context is never enabled merely by enabling the scheduler.

For lifeOS writes, prefer a narrow operation allowlist such as:

```yaml
autonomy:
  enabled: true
  allowed_categories: [analysis, notifications]
  lifeos_write_operations: []
```

Add `lifeos_writes` and explicit operation names only after reviewing the
workflow arguments and the audit records.
