# Scheduling, workflows, and autonomy

ARIA includes an in-process scheduler for reminders, briefings, monitoring, timers, and carefully scoped autonomous actions. The scheduler persists state in SQLite, but it is not an external daemon: **jobs execute only while ARIA is running.**

## Architecture

```text
YAML workflows / built-in jobs
             │
             ▼
      SchedulerService
             │
       SQLite store
             │
      ┌──────┴───────┐
      │              │
   analysis      autonomous action
      │              │
      ▼              ▼
 ARIA narration   allowlist checks
      │              │
      └──────┬───────┘
             ▼
 NotificationService + active chat
```

## Enabling the scheduler

A conservative starting configuration is:

```yaml
scheduler:
  enabled: true
  analysis_enabled: true

autonomy:
  enabled: false
  allowed_categories: []
```

This permits read-only analysis/briefing work while keeping consequential autonomy disabled.

## Persistence

Scheduler state is stored under the configured scheduler database, normally:

```text
data/scheduler/scheduler.sqlite3
```

Jobs, timers, and audit entries survive restarts. Execution itself does not continue while ARIA is closed.

Every attempted scheduled action is recorded, including blocked and failed attempts. This makes the scheduler's behavior inspectable rather than an invisible background process.

## Cron format

Workflow cron expressions use five local-time fields:

```text
minute hour day-of-month month weekday
```

Supported forms include:

- `*`
- `*/step`
- comma-separated values
- ranges
- single integers

Weekday uses standard cron numbering: `0` or `7` is Sunday and `6` is Saturday.

Example:

```yaml
cron: "0 17 * * 1-5"
```

means 17:00 Monday through Friday.

## Misfire policies

Each workflow can use:

```yaml
misfire_policy: skip
```

or:

```yaml
misfire_policy: run_once
```

`skip` does not replay missed executions after restart. `run_once` replays one missed execution when ARIA starts.

## Autonomy categories

The scheduler distinguishes read-only analysis from consequential operations.

### `analysis`

Read-only briefings and monitoring such as deadline, goal, calendar, routine, and profile checks. These require `scheduler.analysis_enabled`.

### `lifeos_writes`

Writes to the LifeOS integration. These require autonomy to be enabled, the category to be allow-listed, and the requested operation to appear in `autonomy.lifeos_write_operations`.

### `notifications`

Desktop notifications and optional TTS delivery. These are still explicit scheduled actions and are governed by the autonomy category boundary.

### `computer_control`

Workflow-triggered ARIA desktop/browser tool calls. This category should be treated as highly trusted because it can affect the local machine or authenticated browser state.

### `timers`

Scheduled timer creation/control.

## Workflows

ARIA loads YAML workflow files from `scheduler.workflow_directory` at startup. A minimal workflow is:

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

See [`workflows/README.md`](../../workflows/README.md) for the complete current schema and examples.

## Built-in briefings

The scheduler supports morning, afternoon, and end-of-day briefings plus periodic deadline, goal, calendar, and profile checks. Briefings can include configured SearXNG queries.

Calendar conflicts are surfaced as deterministic signals before background-model prose is generated. This is preferable to asking a language model to invent calendar state from memory.

## Timers

Timers can be controlled interactively or through model tools:

```text
/timer create focus pomodoro 1500
/timer start <id>
/timer pause <id>
/timer resume <id>
/timer restart <id>
/timer finish <id>
/timer list
```

Timers can be persistent or session-oriented depending on how they are created.

## Notifications

`NotificationService` can deliver scheduler results through the configured desktop backend and, when enabled, speech. While a UI is active, scheduler results are also forwarded into the live chat transcript.

The notification configuration supports `notify-send` or D-Bus backends, urgency, timeout, and optional TTS.

## Periodic screen context

Periodic screen analysis is deliberately separate from ordinary scheduler activation. It requires the vision periodic-screen setting in addition to scheduler/autonomy configuration.

When enabled, the scheduler creates a `screen_check` analysis job using the configured `vision.periodic_screen_cron`. Screenshots are temporary and are not retained by default.

## Safe autonomy progression

Use this progression:

1. Scheduler disabled.
2. Scheduler enabled with analysis disabled.
3. Read-only analysis enabled.
4. Notifications explicitly allowed.
5. Narrow LifeOS writes allowed one operation at a time.
6. Computer control enabled only for workflows you have reviewed.

Example conservative policy:

```yaml
autonomy:
  enabled: true
  allowed_categories:
    - analysis
    - notifications
  lifeos_write_operations: []
```

Do not add `computer_control` or broad LifeOS write permissions merely because a workflow file exists. Review each action's exact arguments and expected side effects first.

## Scheduler debugging

Use `/scheduler` to inspect scheduler state and allowlist status. If a workflow does not execute:

1. Confirm `scheduler.enabled`.
2. Confirm the YAML file is in the configured workflow directory.
3. Check the cron expression and local timezone.
4. Check `enabled: true` on the workflow.
5. Check category permissions.
6. Inspect scheduler audit records.
7. Inspect `data/logs/aria.log` for runtime errors.

Remember that closing ARIA stops the in-process scheduler. Persistent jobs are not equivalent to a system service or external cron daemon.
