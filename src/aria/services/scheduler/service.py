"""Persistent in-process scheduler for proactive ARIA work.

Jobs and timers are durable in SQLite. The worker is deliberately in-process
(the configured deployment choice), so it stops with ARIA and resumes from its
stored state on the next launch.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

import yaml

from ...config import AutonomyConfig, SchedulerConfig
from ...logging.setup import log_error, log_info


@dataclass(frozen=True)
class CronSchedule:
    """Minimal standard five-field cron matcher: minute hour dom month dow."""

    fields: tuple[frozenset[int], ...]

    @classmethod
    def parse(cls, expression: str) -> "CronSchedule":
        parts = expression.split()
        if len(parts) != 5:
            raise ValueError("cron must contain five fields: minute hour day month weekday")
        # Cron permits both 0 and 7 for Sunday in the weekday field.
        limits = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 7))
        return cls(tuple(frozenset(cls._field(part, low, high)) for part, (low, high) in zip(parts, limits)))

    @staticmethod
    def _field(value: str, low: int, high: int) -> set[int]:
        result: set[int] = set()
        for item in value.split(","):
            item = item.strip()
            if item == "*":
                result.update(range(low, high + 1))
                continue
            if item.startswith("*/"):
                step = int(item[2:])
                if step <= 0:
                    raise ValueError("cron step must be positive")
                result.update(range(low, high + 1, step))
                continue
            if "-" in item:
                start, end = (int(part) for part in item.split("-", 1))
                if start > end:
                    raise ValueError("cron range start must not exceed end")
                result.update(range(start, end + 1))
                continue
            result.add(int(item))
        if not result or any(value < low or value > high for value in result):
            raise ValueError(f"cron field out of range {low}-{high}")
        if low == 0 and high == 7 and 7 in result:
            result.remove(7)
            result.add(0)
        return result

    def matches(self, moment: datetime) -> bool:
        values = (moment.minute, moment.hour, moment.day, moment.month, (moment.weekday() + 1) % 7)
        minute, hour, day, month, weekday = self.fields
        # Standard cron uses OR between restricted day-of-month and weekday
        # fields, while wildcard fields behave as an unrestricted side.
        day_match = moment.day in day
        weekday_match = values[4] in weekday
        dom_wild = len(day) == 31
        dow_wild = len(weekday) == 7
        calendar_match = (day_match or weekday_match) if not (dom_wild or dow_wild) else (day_match and weekday_match)
        return (
            values[0] in minute
            and values[1] in hour
            and values[3] in month
            and calendar_match
        )


@dataclass(frozen=True)
class ScheduledJob:
    id: str
    name: str
    schedule: str
    category: str
    action: dict[str, Any]
    misfire_policy: str = "skip"
    enabled: bool = True


class SchedulerStore:
    """SQLite persistence for jobs, timers, and an immutable action audit."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("""
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY, name TEXT UNIQUE NOT NULL, schedule TEXT NOT NULL,
                category TEXT NOT NULL, action_json TEXT NOT NULL, misfire_policy TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1, last_run TEXT, last_minute TEXT
            )
        """)
        self._db.execute("""
            CREATE TABLE IF NOT EXISTS timers (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, kind TEXT NOT NULL,
                duration_seconds REAL NOT NULL, started_at TEXT, elapsed_seconds REAL NOT NULL DEFAULT 0,
                state TEXT NOT NULL, persistent INTEGER NOT NULL DEFAULT 1,
                updated_at TEXT NOT NULL
            )
        """)
        self._db.execute("""
            CREATE TABLE IF NOT EXISTS autonomous_audit (
                id TEXT PRIMARY KEY, occurred_at TEXT NOT NULL, category TEXT NOT NULL,
                action TEXT NOT NULL, target TEXT NOT NULL, status TEXT NOT NULL,
                details_json TEXT NOT NULL
            )
        """)
        # Session-only timers are scoped to the process that created them.
        # Remove them when the durable store is reopened so they cannot become
        # accidental persistent reminders after a restart.
        self._db.execute("DELETE FROM timers WHERE persistent=0")
        self._db.commit()

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def upsert_job(self, job: ScheduledJob) -> None:
        with self._lock:
            self._db.execute(
                """INSERT INTO jobs(id,name,schedule,category,action_json,misfire_policy,enabled)
                   VALUES(?,?,?,?,?,?,?) ON CONFLICT(name) DO UPDATE SET
                   schedule=excluded.schedule, category=excluded.category,
                   action_json=excluded.action_json, misfire_policy=excluded.misfire_policy,
                   enabled=excluded.enabled""",
                (job.id, job.name, job.schedule, job.category, json.dumps(job.action), job.misfire_policy, int(job.enabled)),
            )
            self._db.commit()

    def jobs(self) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._db.execute("SELECT * FROM jobs ORDER BY name").fetchall())

    def mark_job_run(self, job_id: str, moment: datetime) -> None:
        with self._lock:
            self._db.execute("UPDATE jobs SET last_run=?, last_minute=? WHERE id=?", (moment.isoformat(), moment.strftime("%Y-%m-%dT%H:%M"), job_id))
            self._db.commit()

    def audit(self, category: str, action: str, target: str, status: str, details: dict[str, Any]) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO autonomous_audit VALUES(?,?,?,?,?,?,?)",
                (uuid.uuid4().hex, datetime.now().astimezone().isoformat(), category, action, target, status, json.dumps(details, default=str)),
            )
            self._db.commit()

    def audit_entries(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(row) for row in self._db.execute("SELECT * FROM autonomous_audit ORDER BY occurred_at DESC LIMIT ?", (limit,)).fetchall()]

    def remove_session_timers(self) -> int:
        """Discard timers explicitly marked session-only during startup.

        A session timer must not silently become a persistent reminder merely
        because ARIA was restarted. Persistent timers retain their running
        state and are recovered from their stored timestamps.
        """
        with self._lock:
            cursor = self._db.execute("DELETE FROM timers WHERE persistent=0")
            self._db.commit()
            return int(cursor.rowcount)

    def add_timer(self, name: str, kind: str, duration: float, persistent: bool) -> str:
        timer_id = uuid.uuid4().hex
        now = datetime.now().astimezone().isoformat()
        with self._lock:
            self._db.execute("INSERT INTO timers VALUES(?,?,?,?,?,?,?,?,?)", (timer_id, name, kind, duration, None, 0.0, "paused", int(persistent), now))
            self._db.commit()
        return timer_id

    def timer(self, timer_id: str) -> sqlite3.Row | None:
        with self._lock:
            return self._db.execute("SELECT * FROM timers WHERE id=?", (timer_id,)).fetchone()

    def timers(self) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._db.execute("SELECT * FROM timers WHERE state NOT IN ('finished','cancelled') ORDER BY updated_at DESC").fetchall())

    def update_timer(self, timer_id: str, *, state: str | None = None, started_at: str | None = None, elapsed: float | None = None) -> None:
        row = self.timer(timer_id)
        if row is None:
            raise ValueError(f"unknown timer: {timer_id}")
        with self._lock:
            self._db.execute(
                "UPDATE timers SET state=?, started_at=?, elapsed_seconds=?, updated_at=? WHERE id=?",
                (state or row["state"], started_at if started_at is not None else row["started_at"], elapsed if elapsed is not None else row["elapsed_seconds"], datetime.now().astimezone().isoformat(), timer_id),
            )
            self._db.commit()


def _interval_cron(minutes: int) -> str:
    """Represent a positive minute interval using valid five-field cron."""
    if minutes <= 59:
        return f"*/{minutes} * * * *"
    if minutes % 60 == 0 and minutes // 60 <= 23:
        return f"0 */{minutes // 60} * * *"
    raise ValueError("scheduler check intervals must be <=59 minutes or whole hours <=23")


class SchedulerService:
    """Run durable cron jobs and timers on a daemon worker thread."""

    def __init__(
        self,
        config: SchedulerConfig,
        autonomy: AutonomyConfig,
        action_runner: Callable[[str, dict[str, Any]], dict[str, Any]],
        notify: Callable[[str, str, str], None],
        narrate: Callable[[str], str] | None = None,
    ) -> None:
        self.config = config
        self.autonomy = autonomy
        self.action_runner = action_runner
        self.notify = notify
        self.narrate = narrate
        self.store = SchedulerStore(config.database)
        # Session-owned timers intentionally do not survive an ARIA restart.
        removed = self.store.remove_session_timers()
        if removed:
            log_info(f"Scheduler: removed {removed} session-only timer(s) from the previous run")
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._load_workflows()

    def _load_workflows(self) -> None:
        directory = self.config.workflow_directory
        directory.mkdir(parents=True, exist_ok=True)
        for path in sorted(directory.glob("*.yaml")) + sorted(directory.glob("*.yml")):
            try:
                raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
                for item in raw.get("workflows", []) if isinstance(raw, dict) else []:
                    self._register_raw(item)
            except (OSError, yaml.YAMLError, ValueError) as exc:
                log_error(f"Scheduler: skipped workflow file {path}: {exc}")
        if self.config.default_workflows:
            periods = ("morning", "afternoon", "end_of_day")
            briefing_jobs = []
            for period, clock in zip(periods, self.config.briefing_times):
                hour, minute = (int(value) for value in clock.split(":", 1))
                briefing_jobs.append((f"{minute} {hour} * * *", period))
            defaults = [
                ("morning_briefing", briefing_jobs[0][0], "analysis", {"type": "briefing", "period": briefing_jobs[0][1]}),
                ("afternoon_briefing", briefing_jobs[1][0], "analysis", {"type": "briefing", "period": briefing_jobs[1][1]}),
                ("evening_briefing", briefing_jobs[2][0], "analysis", {"type": "briefing", "period": briefing_jobs[2][1]}),
                ("deadline_check", _interval_cron(self.config.deadline_check_minutes), "analysis", {"type": "deadline_check"}),
                ("goal_check", _interval_cron(self.config.goal_check_minutes), "analysis", {"type": "goal_check"}),
                ("calendar_check", _interval_cron(self.config.calendar_check_minutes), "analysis", {"type": "calendar_check"}),
                ("profile_check", "15 3 * * *", "analysis", {"type": "profile_check"}),
            ]
            for name, schedule, category, action in defaults:
                self.store.upsert_job(ScheduledJob(name, name, schedule, category, action, self.config.default_misfire_policy))

    def _register_raw(self, item: Any) -> None:
        if not isinstance(item, dict):
            raise ValueError("workflow must be an object")
        name = item.get("name")
        schedule = item.get("cron")
        category = item.get("category", "analysis")
        action = item.get("action")
        if not isinstance(name, str) or not name.strip() or not isinstance(schedule, str) or not isinstance(category, str) or not isinstance(action, dict):
            raise ValueError("workflow needs name, cron, category, and action")
        CronSchedule.parse(schedule)
        misfire = str(item.get("misfire_policy", self.config.default_misfire_policy))
        if misfire not in {"skip", "run_once"}:
            raise ValueError("misfire_policy must be skip or run_once")
        self.store.upsert_job(ScheduledJob(uuid.uuid4().hex, name.strip(), schedule, category.strip(), action, misfire, bool(item.get("enabled", True))))

    def start(self) -> None:
        if self._thread is not None:
            return
        # A service may be constructed before the UI is ready; make sure a
        # previous stop signal cannot cause a newly started worker to exit
        # without processing its first cron tick.
        self._stop.clear()
        self._recover_misfires(datetime.now().astimezone())
        self._thread = threading.Thread(target=self._run, name="aria-scheduler", daemon=True)
        self._thread.start()
        log_info("Scheduler: started in-process")

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
        self.store.close()
        self._thread = None

    def _recover_misfires(self, moment: datetime) -> None:
        """Run eligible persistent jobs once after a restart."""
        for row in self.store.jobs():
            if not row["enabled"] or row["misfire_policy"] != "run_once" or not row["last_run"]:
                continue
            try:
                previous = datetime.fromisoformat(str(row["last_run"]))
                if previous < moment.replace(second=0, microsecond=0):
                    self._execute_job(row, moment)
                    self.store.mark_job_run(str(row["id"]), moment)
            except (TypeError, ValueError) as exc:
                log_error(f"Scheduler: invalid last_run for {row['name']}: {exc}")

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as exc:
                log_error(f"Scheduler: tick failed: {type(exc).__name__}: {exc}")
            self._stop.wait(max(1, self.config.poll_seconds))

    def tick(self, now: datetime | None = None) -> None:
        moment = now or datetime.now().astimezone()
        minute = moment.strftime("%Y-%m-%dT%H:%M")
        for row in self.store.jobs():
            if not row["enabled"] or row["last_minute"] == minute:
                continue
            try:
                if not CronSchedule.parse(str(row["schedule"])).matches(moment):
                    continue
                self._execute_job(row, moment)
            finally:
                self.store.mark_job_run(str(row["id"]), moment)
        self._tick_timers(moment)

    def _allowed(self, category: str) -> bool:
        # Briefings and monitoring are read-only analysis. They must not be
        # blocked by the write/autonomy allowlist, otherwise the shipped
        # default briefs silently become "blocked" when autonomy is false.
        if category == "analysis":
            return self.config.analysis_enabled
        return self.autonomy.enabled and category in set(self.autonomy.allowed_categories)

    def _execute_job(self, row: sqlite3.Row, moment: datetime) -> None:
        category = str(row["category"])
        action = json.loads(str(row["action_json"]))
        name = str(row["name"])
        if not self._allowed(category):
            self.store.audit(category, "job", name, "blocked", {"reason": "category not allowlisted", "action": action})
            return
        try:
            result = self.action_runner(category, action)
            self.store.audit(category, "job", name, "success", result)
            message = str(result.get("notification") or "")
            # Briefings deliberately enter the normal conversational agent as
            # a user turn. This preserves ARIA's persona, memory, tools, and
            # telemetry and prevents raw source JSON from reaching the UI.
            agent_input = result.get("agent_input")
            if not message and isinstance(agent_input, str):
                if self.narrate is None:
                    raise RuntimeError("briefing narration is not configured")
                message = self.narrate(agent_input)
            if message:
                self.notify(f"ARIA · {name}", message, str(result.get("urgency", "normal")))
        except Exception as exc:
            self.store.audit(category, "job", name, "error", {"error": str(exc)})
            log_error(f"Scheduler: job {name} failed: {exc}")

    def create_timer(self, name: str, kind: str, duration_seconds: float, persistent: bool = True) -> str:
        if not name.strip() or not kind.strip():
            raise ValueError("timer name and kind must be non-empty")
        if duration_seconds < 0 or (duration_seconds == 0 and kind != "stopwatch"):
            raise ValueError("duration must be positive, except for a stopwatch")
        timer_id = self.store.add_timer(name, kind, duration_seconds, persistent)
        self.store.audit("timers", "create", timer_id, "success", {"name": name, "kind": kind, "duration": duration_seconds})
        return timer_id

    def control_timer(self, timer_id: str, command: str) -> dict[str, Any]:
        row = self.store.timer(timer_id)
        if row is None:
            raise ValueError(f"unknown timer: {timer_id}")
        now = datetime.now().astimezone()
        state = str(row["state"])
        elapsed = float(row["elapsed_seconds"])
        if state == "running" and row["started_at"]:
            elapsed += max(0.0, (now - datetime.fromisoformat(str(row["started_at"]))).total_seconds())
        if command in {"start", "resume"}:
            self.store.update_timer(timer_id, state="running", started_at=now.isoformat(), elapsed=elapsed)
        elif command == "pause":
            self.store.update_timer(timer_id, state="paused", started_at=None, elapsed=elapsed)
        elif command == "restart":
            self.store.update_timer(timer_id, state="running", started_at=now.isoformat(), elapsed=0.0)
        elif command in {"finish", "stop"}:
            self.store.update_timer(timer_id, state="finished", started_at=None, elapsed=elapsed)
        elif command == "cancel":
            self.store.update_timer(timer_id, state="cancelled", started_at=None, elapsed=elapsed)
        else:
            raise ValueError("timer command must be start, pause, resume, restart, finish, stop, or cancel")
        self.store.audit("timers", command, timer_id, "success", {})
        return self.timer_status(timer_id)

    def timer_status(self, timer_id: str) -> dict[str, Any]:
        row = self.store.timer(timer_id)
        if row is None:
            raise ValueError(f"unknown timer: {timer_id}")
        elapsed = float(row["elapsed_seconds"])
        if row["state"] == "running" and row["started_at"]:
            elapsed += max(0.0, (datetime.now().astimezone() - datetime.fromisoformat(str(row["started_at"]))).total_seconds())
        return {"id": timer_id, "name": row["name"], "kind": row["kind"], "state": row["state"], "elapsed_seconds": round(elapsed, 1), "duration_seconds": row["duration_seconds"], "persistent": bool(row["persistent"])}

    def list_timers(self) -> list[dict[str, Any]]:
        return [self.timer_status(str(row["id"])) for row in self.store.timers()]

    def _tick_timers(self, now: datetime) -> None:
        for row in self.store.timers():
            if row["state"] != "running" or not row["started_at"]:
                continue
            elapsed = float(row["elapsed_seconds"]) + max(0.0, (now - datetime.fromisoformat(str(row["started_at"]))).total_seconds())
            duration = float(row["duration_seconds"])
            if duration and elapsed >= duration:
                timer_id = str(row["id"])
                self.store.update_timer(timer_id, state="finished", started_at=None, elapsed=duration)
                self.store.audit("timers", "fire", timer_id, "success", {"name": row["name"], "kind": row["kind"]})
                self.notify(f"ARIA · {row['kind']}", f"Timer finished: {row['name']}", "normal")


def register_scheduler_tools(registry: Any, service: SchedulerService) -> None:
    """Expose timer/job controls to the interactive ARIA agent."""
    from ...tools.core.base import Tool, ToolContext, ToolResult

    def create(args: dict[str, Any], _context: ToolContext) -> ToolResult:
        try:
            timer_id = service.create_timer(
                str(args.get("name", "")), str(args.get("kind", "timer")),
                float(args.get("duration_seconds", 0)), bool(args.get("persistent", True)),
            )
            return ToolResult(json.dumps(service.timer_status(timer_id)))
        except Exception as exc:
            return ToolResult(f"Timer creation failed: {type(exc).__name__}: {exc}", is_error=True)

    def control(args: dict[str, Any], _context: ToolContext) -> ToolResult:
        try:
            return ToolResult(json.dumps(service.control_timer(str(args.get("timer_id", "")), str(args.get("command", "")))))
        except Exception as exc:
            return ToolResult(f"Timer control failed: {type(exc).__name__}: {exc}", is_error=True)

    def status(args: dict[str, Any], _context: ToolContext) -> ToolResult:
        try:
            timer_id = args.get("timer_id")
            value = service.list_timers() if timer_id is None else service.timer_status(str(timer_id))
            return ToolResult(json.dumps(value))
        except Exception as exc:
            return ToolResult(f"Timer status failed: {type(exc).__name__}: {exc}", is_error=True)

    registry.register(Tool(
        "timer_create", "Create a persistent or session-owned reminder, alarm, pomodoro, or stopwatch.",
        {"type": "object", "properties": {"name": {"type": "string"}, "kind": {"type": "string", "enum": ["reminder", "alarm", "pomodoro", "stopwatch", "timer"]}, "duration_seconds": {"type": "number", "minimum": 0}, "persistent": {"type": "boolean"}}, "required": ["name", "kind"], "additionalProperties": False}, create,
    ))
    registry.register(Tool(
        "timer_control", "Start, pause, resume, restart, finish, stop, or cancel a timer.",
        {"type": "object", "properties": {"timer_id": {"type": "string"}, "command": {"type": "string", "enum": ["start", "pause", "resume", "restart", "finish", "stop", "cancel"]}}, "required": ["timer_id", "command"], "additionalProperties": False}, control,
    ))
    registry.register(Tool(
        "timer_status", "List active timers or inspect one timer.",
        {"type": "object", "properties": {"timer_id": {"type": "string"}}, "additionalProperties": False}, status,
    ))
