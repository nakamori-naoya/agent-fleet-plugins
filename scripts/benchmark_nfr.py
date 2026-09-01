#!/usr/bin/env python3
"""Reproducible local benchmarks for the documented Core performance targets."""

from __future__ import annotations

import argparse
import importlib.util
import json
import math
import platform
import sqlite3
import tempfile
import time
from pathlib import Path


ROOT = Path(__file__).parents[1]
MODULE_PATH = ROOT / "plugins" / "agent-fleet-core" / "core" / "fleet_control.py"
SPEC = importlib.util.spec_from_file_location("fleet_control_benchmark", MODULE_PATH)
fleet_control = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(fleet_control)


def fleet_document(fleet_id: str, task_count: int) -> dict:
    members = [
        {"agent_ref": "manager", "role_ref": "manager@1"},
        {"agent_ref": "worker-1", "role_ref": "worker@1"},
        {"agent_ref": "worker-2", "role_ref": "worker@1"},
        {"agent_ref": "advisor", "role_ref": "advisor@1"},
        {"agent_ref": "reviewer", "role_ref": "reviewer@1"},
    ]
    tasks = []
    workers = [member["agent_ref"] for member in members if member["agent_ref"] != "manager"]
    for index in range(task_count):
        assignee = workers[index % len(workers)]
        tasks.append(
            {
                "id": f"task-{index:03d}",
                "assignee": assignee,
                "depends_on": [],
                "instructions": "Perform the benchmark task.",
                "expected_output": "A benchmark progress record.",
                "completion_criteria": ["The report is durably recorded."],
            }
        )
    return {
        "apiVersion": "fleet.harness/v1",
        "kind": "Fleet",
        "metadata": {"id": fleet_id},
        "spec": {
            "objective": "Measure local Agent Fleet Core performance.",
            "completion_criteria": ["All benchmark samples are recorded."],
            "stop_conditions": ["Any operation fails."],
            "members": members,
            "tasks": tasks,
            "collaboration": {"manager": "manager"},
            "view": {"profile_ref": "local/benchmark@1"},
        },
    }


def elapsed_ms(operation) -> float:
    started = time.perf_counter_ns()
    operation()
    return (time.perf_counter_ns() - started) / 1_000_000


def distribution(samples: list[float], target_ms: float) -> dict:
    ordered = sorted(samples)
    p95_index = math.ceil(len(ordered) * 0.95) - 1
    return {
        "samples": len(ordered),
        "median_ms": round(ordered[len(ordered) // 2], 3),
        "p95_ms": round(ordered[p95_index], 3),
        "max_ms": round(ordered[-1], 3),
        "target_ms": target_ms,
        "passed": ordered[p95_index] <= target_ms,
    }


def run(samples: int) -> dict:
    with tempfile.TemporaryDirectory(prefix="agent-fleet-benchmark-") as temporary:
        db_path = Path(temporary) / "core.sqlite3"
        store = fleet_control.FleetStore(db_path)
        fleet_count = 3
        task_count = 100
        reports_per_task = 20
        for fleet_index in range(fleet_count):
            fleet_id = f"benchmark-{fleet_index}"
            store.initialize(fleet_document(fleet_id, task_count))
            for task_index in range(task_count):
                task_id = f"task-{task_index:03d}"
                assignee = ("worker-1", "worker-2", "advisor", "reviewer")[task_index % 4]
                store.assign(
                    fleet_id,
                    task_id,
                    assignee,
                    "manager",
                    f"assign:{fleet_id}:{task_id}",
                )
                store.transition_task(fleet_id, task_id, "running", assignee)
                for report_index in range(reports_per_task):
                    store.report_progress(
                        fleet_id,
                        task_id,
                        assignee,
                        f"report:{fleet_id}:{task_id}:{report_index:02d}",
                        {"summary": "benchmark", "milestone": report_index},
                        "2099-01-01T00:00:00+00:00",
                    )

        for warmup in range(10):
            store.status("benchmark-0")
            store.check_report_deadlines(
                "benchmark-0", "2098-01-01T00:00:00+00:00"
            )
            store.report_progress(
                "benchmark-0",
                "task-000",
                "worker-1",
                f"warmup:{warmup}",
                {"summary": "warmup", "sample": warmup},
                "2099-01-01T00:00:00+00:00",
            )

        report_times = []
        for sample in range(samples):
            report_times.append(
                elapsed_ms(
                    lambda sample=sample: store.report_progress(
                        "benchmark-0",
                        "task-000",
                        "worker-1",
                        f"measured:{sample}",
                        {"summary": "measured", "sample": sample},
                        "2099-01-01T00:00:00+00:00",
                    )
                )
            )
        status_times = [
            elapsed_ms(lambda: store.status("benchmark-0")) for _ in range(samples)
        ]
        deadline_check_times = [
            elapsed_ms(
                lambda: store.check_report_deadlines(
                    "benchmark-0", "2098-01-01T00:00:00+00:00"
                )
            )
            for _ in range(samples)
        ]
        with sqlite3.connect(db_path) as db:
            counts = {
                table: db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                for table in ("fleets", "members", "tasks", "task_reports", "events", "outbox")
            }
        return {
            "environment": {
                "platform": platform.platform(),
                "python": platform.python_version(),
                "sqlite": sqlite3.sqlite_version,
                "database": "local temporary filesystem, WAL",
            },
            "fixture": {
                "fleets": fleet_count,
                "members_per_fleet": 5,
                "tasks_per_fleet": task_count,
                "reports_per_task": reports_per_task,
                "rows": counts,
            },
            "performance-01_report_to_notification": distribution(report_times, 100.0),
            "performance-03_status": distribution(status_times, 200.0),
            "performance-04_progress_check_core_portion": distribution(
                deadline_check_times, 1_200.0
            ),
        }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--samples", type=int, default=100)
    args = parser.parse_args()
    if args.samples < 100:
        parser.error("--samples must be at least 100")
    result = run(args.samples)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if all(
        metric["passed"]
        for name, metric in result.items()
        if name.startswith("performance-")
    ) else 1


if __name__ == "__main__":
    raise SystemExit(main())
