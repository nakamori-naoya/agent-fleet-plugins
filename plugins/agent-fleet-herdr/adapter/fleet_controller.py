#!/usr/bin/env python3
"""Paneを持たないAgent Fleetの一回実行配送制御。"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence


Runner = Callable[..., subprocess.CompletedProcess[str]]


class FleetControllerError(RuntimeError):
    """再実行可否を判断できる配送制御エラー。"""


class FleetController:
    def __init__(
        self,
        core_command: Sequence[str],
        herdr_command: Sequence[str],
        *,
        runner: Runner = subprocess.run,
    ):
        self.core_command = tuple(core_command)
        self.herdr_command = tuple(herdr_command)
        self.runner = runner

    def _run_json(
        self, argv: Sequence[str], context: str, *, timeout_seconds: int = 30
    ) -> Mapping[str, Any]:
        completed = self.runner(
            list(argv), capture_output=True, text=True, timeout=timeout_seconds
        )
        if completed.returncode != 0:
            raise FleetControllerError(
                f"{context} failed: {completed.stderr.strip() or 'unknown error'}"
            )
        try:
            document = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise FleetControllerError(f"{context} returned invalid JSON") from exc
        if not isinstance(document, Mapping) or document.get("ok") is not True:
            raise FleetControllerError(f"{context} did not return an ok result")
        return document

    def run_once(
        self,
        *,
        core_db: str,
        herdr_db: str,
        fleet_id: str,
        worker_id: str,
        lease_seconds: int = 60,
    ) -> dict[str, Any]:
        warnings = []
        try:
            self._run_json(
                [
                    *self.core_command,
                    "--db",
                    core_db,
                    "progress.check",
                    "--fleet",
                    fleet_id,
                ],
                "Core progress deadline check",
            )
        except FleetControllerError as exc:
            # Deadline observation must not starve already durable delivery work.
            warnings.append(str(exc))
        claim = self._run_json(
            [
                *self.core_command,
                "--db",
                core_db,
                "delivery.claim",
                "--fleet",
                fleet_id,
                "--worker-id",
                worker_id,
                "--lease-seconds",
                str(lease_seconds),
            ],
            "Core delivery claim",
        ).get("result")
        if claim is None:
            return {"status": "idle", "fleet_id": fleet_id, "warnings": warnings}
        if not isinstance(claim, Mapping):
            raise FleetControllerError("Core delivery claim result must be an object")
        command = claim.get("command")
        delivery = claim.get("delivery")
        if not isinstance(command, Mapping) or not isinstance(delivery, Mapping):
            raise FleetControllerError("Core delivery claim is missing command or delivery")
        metadata = command.get("metadata")
        if not isinstance(metadata, Mapping):
            raise FleetControllerError("claimed command metadata is missing")
        command_id = metadata.get("id")
        lease_token = delivery.get("lease_token")
        if not isinstance(command_id, str) or not isinstance(lease_token, str):
            raise FleetControllerError("claimed command identity or lease token is missing")

        self._run_json(
            [
                *self.core_command,
                "--db",
                core_db,
                "delivery.begin",
                "--fleet",
                fleet_id,
                "--command-id",
                command_id,
                "--lease-token",
                lease_token,
            ],
            "Core delivery begin",
        )

        dispatch_argv = [
            *self.herdr_command,
            "--state-db",
            herdr_db,
            "dispatch",
            "--request-json",
            json.dumps(command, ensure_ascii=False, sort_keys=True),
            "--until-started",
            "--execute",
        ]
        try:
            dispatched = self._run_json(
                dispatch_argv, "Herdr dispatch", timeout_seconds=40
            ).get("result")
            if not isinstance(dispatched, Mapping):
                raise FleetControllerError("Herdr dispatch result must be an object")
            dispatch_status = dispatched.get("status")
            if dispatch_status == "submitted":
                delivery_result = "unknown"
                detail = (
                    "Herdr submitted the prompt, but only the Agent Fleet hook can "
                    "confirm receipt"
                )
            elif dispatch_status == "unknown":
                delivery_result = "unknown"
                detail = str(dispatched.get("reason") or "Herdr delivery result is unknown")
            else:
                delivery_result = "retry"
                detail = f"unexpected Herdr dispatch status: {dispatch_status}"
        except FleetControllerError as exc:
            delivery_result = "unknown"
            detail = str(exc)

        recorded = self._run_json(
            [
                *self.core_command,
                "--db",
                core_db,
                "delivery.result",
                "--fleet",
                fleet_id,
                "--command-id",
                command_id,
                "--lease-token",
                lease_token,
                "--result",
                delivery_result,
                "--detail",
                detail,
            ],
            "Core delivery result",
        ).get("result")
        if not isinstance(recorded, Mapping):
            raise FleetControllerError("Core delivery result must be an object")
        return {
            **dict(recorded),
            "delivery_scope": "hook_receipt",
            "warnings": warnings,
        }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="fleet-controller")
    adapter_root = Path(__file__).resolve().parent
    core_default = os.environ.get("AGENT_FLEET_CORE_COMMAND") or shutil.which(
        "fleet-control"
    ) or "fleet-control"
    herdr_default = adapter_root / "scripts" / "fleet-herdr"
    parser.add_argument("--core-command", default=str(core_default))
    parser.add_argument("--core-db", required=True)
    parser.add_argument("--herdr-command", default=str(herdr_default))
    parser.add_argument("--herdr-db", required=True)
    parser.add_argument("--fleet", required=True)
    parser.add_argument("--worker-id", required=True)
    parser.add_argument("--lease-seconds", type=int, default=60)
    parser.add_argument("--execute", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.execute:
        print(
            json.dumps(
                {
                    "ok": True,
                    "result": {
                        "mode": "dry-run",
                        "status": "planned",
                        "fleet_id": args.fleet,
                    },
                },
                sort_keys=True,
            )
        )
        return 0
    controller = FleetController([args.core_command], [args.herdr_command])
    try:
        result = controller.run_once(
            core_db=args.core_db,
            herdr_db=args.herdr_db,
            fleet_id=args.fleet,
            worker_id=args.worker_id,
            lease_seconds=args.lease_seconds,
        )
    except (FleetControllerError, OSError, subprocess.SubprocessError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}), file=sys.stderr)
        return 2
    print(json.dumps({"ok": True, "result": result}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
