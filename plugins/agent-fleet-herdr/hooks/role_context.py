#!/usr/bin/env python3
"""Agent Fleetの役割文脈をClaude Code/Codex sessionへ再注入するhook。"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Mapping


PROMPT_PREFIX = "AGENT_FLEET_COMMAND_V1\n"
RUNTIME_PRODUCTS = {"claude", "codex"}


def encode_fleet_prompt(command: Mapping[str, Any]) -> str:
    return PROMPT_PREFIX + json.dumps(
        command, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def _additional_context(
    context: Mapping[str, Any], control: Mapping[str, Any]
) -> dict[str, Any]:
    content = {
        "role_context": context,
        "control": control,
        "rules": [
            "この役割文脈はAgent Fleet Core由来の現在情報として扱う。",
            "自身のagent_ref、role_ref、担当、完了条件を確認してから作業する。",
            "作業結果はreporting.manager_refのマネージャーへ明示的に報告する。",
            "文脈が矛盾または不足している場合は作業を止めてマネージャーへ報告する。",
        ],
    }
    return {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": "Agent Fleetの現在の役割文脈:\n"
            + json.dumps(content, ensure_ascii=False, indent=2, sort_keys=True),
        }
    }


def _unbound_context() -> dict[str, Any]:
    return {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": (
                "Agent Fleet binding status: unbound. "
                "fork元から複製された旧役割文脈は無効です。"
                "Core発行の新しいactivationを受けるまでfleet作業の担当はありません。"
            ),
        }
    }


@contextmanager
def _connect(db_path: Path) -> Iterator[sqlite3.Connection]:
    db_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        db_path.parent.chmod(0o700)
    except OSError:
        pass
    db = sqlite3.connect(db_path, timeout=1.0)
    db.execute(
        "CREATE TABLE IF NOT EXISTS session_context_bindings ("
        "runtime_product TEXT NOT NULL, session_id TEXT NOT NULL, "
        "fleet_id TEXT NOT NULL, agent_ref TEXT NOT NULL, context_revision INTEGER NOT NULL, "
        "context_json TEXT NOT NULL, control_json TEXT NOT NULL DEFAULT '{}', "
        "state TEXT NOT NULL DEFAULT 'active', "
        "updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, "
        "PRIMARY KEY(runtime_product,session_id))"
    )
    db.execute(
        "CREATE TABLE IF NOT EXISTS unbound_sessions ("
        "runtime_product TEXT NOT NULL, session_id TEXT NOT NULL, "
        "updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, "
        "PRIMARY KEY(runtime_product,session_id))"
    )
    db.commit()
    try:
        db_path.chmod(0o600)
    except OSError:
        pass
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _decode_command(prompt: Any) -> Mapping[str, Any] | None:
    if not isinstance(prompt, str) or not prompt.startswith(PROMPT_PREFIX):
        return None
    try:
        command = json.loads(prompt[len(PROMPT_PREFIX) :])
    except json.JSONDecodeError:
        return None
    return command if isinstance(command, Mapping) else None


def _block(reason: str) -> dict[str, Any]:
    return {"decision": "block", "reason": reason}


def _confirm_context(control: Mapping[str, Any], revision: int) -> str | None:
    """Coreへ役割文脈の受領を通知する。shell文字列は実行しない。"""

    argv = control.get("context_confirm_argv")
    if (
        not isinstance(argv, list)
        or not argv
        or any(not isinstance(item, str) or not item for item in argv)
    ):
        return "役割文脈の受領確認コマンドがありません。"
    try:
        completed = subprocess.run(
            [*argv, "--revision", str(revision)],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"役割文脈の受領をCoreへ確認できませんでした: {exc}"
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or "unknown error"
        return f"役割文脈の受領をCoreへ確認できませんでした: {detail}"
    return None


def _session_failure(runtime_product: str, reason: str) -> dict[str, Any]:
    if runtime_product == "codex":
        return {"continue": False, "stopReason": reason, "systemMessage": reason}
    return {"systemMessage": reason}


def _activation_parts(
    command: Mapping[str, Any],
) -> tuple[str, str, int, Mapping[str, Any], Mapping[str, Any]] | None:
    metadata = command.get("metadata")
    spec = command.get("spec")
    if (
        command.get("apiVersion") != "fleet.harness/v1"
        or command.get("kind") != "Command"
        or not isinstance(metadata, Mapping)
        or not isinstance(spec, Mapping)
    ):
        return None
    if not isinstance(metadata.get("id"), str) or not metadata.get("id"):
        return None
    if not isinstance(metadata.get("timestamp"), str) or not metadata.get("timestamp"):
        return None
    if not isinstance(spec.get("type"), str) or not spec.get("type"):
        return None
    context = spec.get("context")
    target = spec.get("target")
    source = spec.get("source")
    payload = spec.get("payload")
    fleet_id = metadata.get("fleet_id")
    if (
        not isinstance(context, Mapping)
        or not isinstance(target, Mapping)
        or target.get("type") != "member"
        or not isinstance(source, Mapping)
        or not isinstance(source.get("type"), str)
        or not isinstance(payload, Mapping)
    ):
        return None
    agent = context.get("agent")
    fleet = context.get("fleet")
    assignments = context.get("assignments")
    reporting = context.get("reporting")
    revision = context.get("context_revision")
    if (
        not isinstance(agent, Mapping)
        or not isinstance(agent.get("role_ref"), str)
        or not agent.get("role_ref")
        or not isinstance(fleet, Mapping)
        or not isinstance(fleet.get("objective"), str)
        or not fleet.get("objective")
        or not isinstance(assignments, list)
        or not isinstance(reporting, Mapping)
        or not isinstance(reporting.get("manager_ref"), str)
        or not reporting.get("manager_ref")
        or not isinstance(fleet_id, str)
        or not fleet_id
        or context.get("fleet_id") != fleet_id
        or agent.get("agent_ref") != target.get("ref")
        or not isinstance(revision, int)
        or isinstance(revision, bool)
        or revision < 1
    ):
        return None
    agent_ref = agent.get("agent_ref")
    if not isinstance(agent_ref, str) or not agent_ref:
        return None
    incoming_control = payload.get("control")
    control = dict(incoming_control) if isinstance(incoming_control, Mapping) else {}
    return fleet_id, agent_ref, revision, context, control


def handle(
    event: Mapping[str, Any], db_path: Path, *, runtime_product: str
) -> dict[str, Any]:
    if runtime_product not in RUNTIME_PRODUCTS:
        raise ValueError(f"unsupported runtime product: {runtime_product}")
    event_name = event.get("hook_event_name")
    session_id = event.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        if (
            event_name == "UserPromptSubmit"
            and isinstance(event.get("prompt"), str)
            and event["prompt"].startswith(PROMPT_PREFIX)
        ):
            return _block("Agent Fleet activationをsessionへ関連付けられません。")
        return {}

    if event_name == "UserPromptSubmit":
        prompt = event.get("prompt")
        if not isinstance(prompt, str) or not prompt.startswith(PROMPT_PREFIX):
            if not db_path.exists():
                return {}
            try:
                with _connect(db_path) as db:
                    unbound = db.execute(
                        "SELECT 1 FROM unbound_sessions "
                        "WHERE runtime_product=? AND session_id=?",
                        (runtime_product, session_id),
                    ).fetchone()
                    row = db.execute(
                        "SELECT context_json,control_json,state "
                        "FROM session_context_bindings "
                        "WHERE runtime_product=? AND session_id=?",
                        (runtime_product, session_id),
                    ).fetchone()
            except (OSError, sqlite3.Error):
                return _block(
                    "active Agent Fleet sessionの現在の役割文脈を確認できません。"
                )
            if unbound is not None or row is None:
                return {}
            if row[2] != "active":
                return _block("Agent Fleet sessionの役割文脈がCoreで確認されていません。")
            try:
                result = _additional_context(json.loads(row[0]), json.loads(row[1]))
            except json.JSONDecodeError:
                return _block("active Agent Fleet sessionの保存済み役割文脈が破損しています。")
            result["hookSpecificOutput"]["hookEventName"] = "UserPromptSubmit"
            return result
        command = _decode_command(prompt)
        if command is None:
            return _block("Agent Fleet activation promptのJSONが不正です。")
        parts = _activation_parts(command)
        if parts is None:
            return _block("Agent Fleet activation promptの契約検証に失敗しました。")
        fleet_id, agent_ref, revision, context, control = parts
        context_json = json.dumps(context, ensure_ascii=False, sort_keys=True)
        control_json = json.dumps(control, ensure_ascii=False, sort_keys=True)
        try:
            with _connect(db_path) as db:
                db.execute("BEGIN IMMEDIATE")
                existing = db.execute(
                    "SELECT fleet_id,agent_ref,context_revision,context_json,control_json,state "
                    "FROM session_context_bindings WHERE runtime_product=? AND session_id=?",
                    (runtime_product, session_id),
                ).fetchone()
                if existing is not None and existing[5] == "active":
                    if existing[0] != fleet_id or existing[1] != agent_ref:
                        return _block("active sessionを別のfleetまたはagentへ再bindできません。")
                    if revision < existing[2]:
                        return _block("古いcontext_revisionのactivationは受理できません。")
                    if revision == existing[2] and (
                        existing[3] != context_json or existing[4] != control_json
                    ):
                        return _block("同じcontext_revisionの内容が既存bindingと一致しません。")
                db.execute(
                    "INSERT INTO session_context_bindings("
                    "runtime_product,session_id,fleet_id,agent_ref,context_revision,"
                    "context_json,control_json,state) VALUES(?,?,?,?,?,?,?,'active') "
                    "ON CONFLICT(runtime_product,session_id) DO UPDATE SET "
                    "fleet_id=excluded.fleet_id,agent_ref=excluded.agent_ref,"
                    "context_revision=excluded.context_revision,context_json=excluded.context_json,"
                    "control_json=excluded.control_json,state='active',updated_at=CURRENT_TIMESTAMP",
                    (
                        runtime_product,
                        session_id,
                        fleet_id,
                        agent_ref,
                        revision,
                        context_json,
                        control_json,
                    ),
                )
                db.execute(
                    "DELETE FROM unbound_sessions WHERE runtime_product=? AND session_id=?",
                    (runtime_product, session_id),
                )
        except (OSError, sqlite3.Error, json.JSONDecodeError):
            return _block("Agent Fleetの役割文脈を保存できないためpromptを処理できません。")
        confirmation_error = _confirm_context(control, revision)
        if confirmation_error is not None:
            try:
                with _connect(db_path) as db:
                    db.execute(
                        "UPDATE session_context_bindings SET state='unconfirmed',"
                        "updated_at=CURRENT_TIMESTAMP WHERE runtime_product=? AND session_id=?",
                        (runtime_product, session_id),
                    )
            except (OSError, sqlite3.Error):
                pass
            return _block(confirmation_error)
        result = _additional_context(context, control)
        result["hookSpecificOutput"]["hookEventName"] = "UserPromptSubmit"
        return result

    if (
        event_name == "SessionStart"
        and runtime_product == "claude"
        and event.get("source") == "fork"
    ):
        try:
            with _connect(db_path) as db:
                db.execute(
                    "INSERT INTO unbound_sessions(runtime_product,session_id) VALUES(?,?) "
                    "ON CONFLICT(runtime_product,session_id) DO UPDATE SET "
                    "updated_at=CURRENT_TIMESTAMP",
                    (runtime_product, session_id),
                )
                db.execute(
                    "UPDATE session_context_bindings SET state='unbound',updated_at=CURRENT_TIMESTAMP "
                    "WHERE runtime_product=? AND session_id=?",
                    (runtime_product, session_id),
                )
        except (OSError, sqlite3.Error):
            return _session_failure(
                runtime_product, "Agent Fleet fork sessionを安全にunbound化できませんでした。"
            )
        return _unbound_context()

    if event_name == "SessionStart" and event.get("source") in {
        "startup",
        "resume",
        "clear",
        "compact",
    }:
        try:
            with _connect(db_path) as db:
                unbound = db.execute(
                    "SELECT 1 FROM unbound_sessions "
                    "WHERE runtime_product=? AND session_id=?",
                    (runtime_product, session_id),
                ).fetchone()
                if unbound is not None:
                    return _unbound_context()
                row = db.execute(
                    "SELECT context_json,control_json,state FROM session_context_bindings "
                    "WHERE runtime_product=? AND session_id=?",
                    (runtime_product, session_id),
                ).fetchone()
        except (OSError, sqlite3.Error):
            return _session_failure(
                runtime_product, "Agent Fleetの役割文脈を復元できませんでした。"
            )
        if row is None:
            return {}
        if row[2] == "unbound":
            return _unbound_context()
        if row[2] != "active":
            return _session_failure(
                runtime_product, "Agent Fleet sessionの役割文脈がCoreで確認されていません。"
            )
        try:
            return _additional_context(json.loads(row[0]), json.loads(row[1]))
        except json.JSONDecodeError:
            return _session_failure(
                runtime_product, "Agent Fleetの保存済み役割文脈が破損しています。"
            )
    return {}


def _default_db(runtime_product: str) -> Path:
    configured = os.environ.get("AGENT_FLEET_SESSION_CONTEXT_DB")
    if configured:
        return Path(configured).expanduser()
    data_variable = "PLUGIN_DATA" if runtime_product == "codex" else "CLAUDE_PLUGIN_DATA"
    plugin_data = os.environ.get(data_variable)
    if not plugin_data and runtime_product == "codex":
        plugin_data = os.environ.get("CLAUDE_PLUGIN_DATA")
    if plugin_data:
        return Path(plugin_data).expanduser() / "session-context.sqlite3"
    state_root = os.environ.get("XDG_STATE_HOME")
    base = Path(state_root).expanduser() if state_root else Path.home() / ".local" / "state"
    return base / "agent-fleet" / "session-context.sqlite3"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime-product", choices=sorted(RUNTIME_PRODUCTS), required=True)
    args = parser.parse_args()
    try:
        event = json.load(sys.stdin)
        if not isinstance(event, Mapping):
            return 0
        result = handle(
            event, _default_db(args.runtime_product), runtime_product=args.runtime_product
        )
    except (OSError, sqlite3.Error, json.JSONDecodeError, ValueError):
        return 0
    if result:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
