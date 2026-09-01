#!/usr/bin/env python3
"""Agent Fleetの役割文脈をClaude Code/Codex sessionへ再注入するhook。"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import sqlite3
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Mapping


PROMPT_PREFIX = "AGENT_FLEET_COMMAND_V1\n"
RUNTIME_PRODUCTS = {"claude", "codex"}
NON_ACTIVATION_COMMAND_TYPES = {
    "fleet.provision",
    "task.assign",
    "message.send",
    "task.report",
    "fleet.reconcile",
}
MAX_CONTEXT_CHARS = 4_000

ROLE_BEHAVIORS = {
    "manager": {
        "mission": "目的と完了条件に照らして各役割の報告を全体進捗へ要約し、次の判断と受容を決める。",
        "must": [
            "報告の事実と未確認範囲を分ける。",
            "判断理由と次に必要な行動を示す。",
            "独立確認が必要な成果は確認役の結果が揃うまで受容しない。",
            "同じ目的の試行が三回失敗したら次の試行を割り当てず利用者判断を求める。",
        ],
        "must_not": ["作業者の成果物を自分で実装しない。", "報告にない事実を推測で補わない。"],
    },
    "worker": {
        "mission": "割り当てられた成果物を作り、検証結果と未確認範囲をマネージャーへ報告する。",
        "must": ["現在の担当と完了条件を確認してから作業する。", "節目、停止、完了、失敗を明示的に報告する。"],
        "must_not": ["他者の担当や完了条件を変更しない。", "自分の成果物を受容済みとして扱わない。"],
    },
    "advisor": {
        "mission": "選択肢、根拠、反例、トレードオフを示してマネージャーの判断材料を増やす。",
        "must": ["推奨案と採らない案の理由を分ける。", "前提の穴と未確認事項を示す。"],
        "must_not": ["成果物を実装しない。", "成果物の受容可否を決めない。"],
    },
    "reviewer": {
        "mission": "作業経緯から独立して成果物を反証し、再現可能な欠陥と未確認範囲を報告する。",
        "must": ["指摘に再現手順と根拠を付ける。", "確認した範囲と未確認範囲を分ける。"],
        "must_not": ["指摘した問題を自分で修正しない。", "好みや総合点だけで受容可否を決めない。"],
    },
    "researcher": {
        "mission": "出典と時点のある事実を調べ、推奨を混ぜずに報告する。",
        "must": ["原典を優先する。", "不明点と調べていない範囲を示す。"],
        "must_not": ["出典のない断定をしない。", "選択肢の最終判断を行わない。"],
    },
}


class ActivationError(RuntimeError):
    """Coreが発行したactivationを安全に取得できない。"""


class CoreTransportError(ActivationError):
    """Coreの処理結果を受信できず、確定有無を再照合する必要がある。"""


def encode_fleet_prompt(command: Mapping[str, Any]) -> str:
    return PROMPT_PREFIX + json.dumps(
        command, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def _additional_context(
    context: Mapping[str, Any],
    control: Mapping[str, Any],
    *,
    command_type: str | None = None,
) -> dict[str, Any]:
    agent = context.get("agent")
    role_ref = agent.get("role_ref") if isinstance(agent, Mapping) else None
    role_id = role_ref.split("@", 1)[0] if isinstance(role_ref, str) else ""
    content = {
        "role_context": context,
        "role_behavior": ROLE_BEHAVIORS.get(role_id),
        "current_command_type": command_type,
        "control": control,
        "rules": [
            "この役割文脈はAgent Fleet Core由来の現在情報として扱う。",
            "自身のagent_ref、role_ref、担当、完了条件を確認してから作業する。",
            "作業結果はreporting.manager_refのマネージャーへ明示的に報告する。",
            "報告時はcontrol.core_commandとcontrol.core_dbを使い、control.reportingのactionとrequired_identityに従う。",
            "文脈が矛盾または不足している場合は作業を止めてマネージャーへ報告する。",
            "context.syncでは役割だけを同期し、待機、sleep、SQLite巡回、担当作業の開始をしない。",
            "current_command_typeがcontext.syncなら、ツールやSkillを呼び出さず、同期完了だけを短く返して直ちに終了する。",
            "task.assignを受けた作業者だけが担当作業を開始する。",
            "task.reportを受けたマネージャーはCore状態と報告を検査し、受理または差し戻しを行う。",
        ],
    }
    return {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": "Agent Fleetの現在の役割文脈:\n"
            + json.dumps(
                content,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
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
def _connect(
    db_path: Path, *, timeout_seconds: float = 1.0
) -> Iterator[sqlite3.Connection]:
    if db_path.is_symlink():
        raise OSError("session context database must not be a symbolic link")
    parent_existed = db_path.parent.exists()
    db_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not parent_existed:
        try:
            db_path.parent.chmod(0o700)
        except OSError:
            pass
    db = sqlite3.connect(db_path, timeout=timeout_seconds)
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
    db.execute(
        "CREATE TABLE IF NOT EXISTS activation_attempts ("
        "runtime_product TEXT NOT NULL, session_id TEXT NOT NULL, command_id TEXT NOT NULL, "
        "fleet_id TEXT NOT NULL, agent_ref TEXT NOT NULL, "
        "updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, "
        "PRIMARY KEY(runtime_product,session_id,command_id))"
    )
    db.execute(
        "CREATE TABLE IF NOT EXISTS command_receipts ("
        "runtime_product TEXT NOT NULL, session_id TEXT NOT NULL, command_id TEXT NOT NULL, "
        "fleet_id TEXT NOT NULL, agent_ref TEXT NOT NULL, state TEXT NOT NULL, "
        "updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, "
        "PRIMARY KEY(runtime_product,session_id,command_id))"
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


def _trusted_core_command() -> list[str]:
    configured = os.environ.get("AGENT_FLEET_CORE_COMMAND")
    if configured:
        argv = shlex.split(configured)
        if argv:
            return argv
    discovered = shutil.which("fleet-control")
    if discovered:
        return [discovered]
    raise ActivationError(
        "fleet-controlが見つかりません。AGENT_FLEET_CORE_COMMANDを設定してください。"
    )


def _trusted_core_db() -> Path:
    configured = os.environ.get("AGENT_FLEET_CORE_DB")
    if configured:
        return Path(configured).expanduser()
    state_root = os.environ.get("XDG_STATE_HOME")
    base = Path(state_root).expanduser() if state_root else Path.home() / ".local/state"
    return base / "agent-fleet" / "core.sqlite3"


def _consume_activation(
    fleet_id: str,
    command_id: str,
    activation_token: str,
    session_id: str,
    runtime_product: str,
) -> Mapping[str, Any]:
    """信頼済みのCore CLIから正本の役割文脈を一度だけ取得する。"""

    argv = [
        *_trusted_core_command(),
        "--db",
        str(_trusted_core_db()),
        "context.consume",
        "--fleet",
        fleet_id,
        "--command-id",
        command_id,
        "--activation-token",
        activation_token,
        "--session-id",
        session_id,
        "--runtime-product",
        runtime_product,
    ]
    try:
        completed = subprocess.run(
            argv,
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ActivationError(
            f"役割文脈をCoreから取得できませんでした: {exc}"
        ) from exc
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or "unknown error"
        raise ActivationError(f"役割文脈をCoreから取得できませんでした: {detail}")
    try:
        document = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise ActivationError("Coreの応答がJSONではありません。") from exc
    result = document.get("result") if isinstance(document, Mapping) else None
    if (
        not isinstance(document, Mapping)
        or document.get("ok") is not True
        or not isinstance(result, Mapping)
    ):
        raise ActivationError("Coreの応答が成功契約を満たしていません。")
    return result


def _command_core_request(
    action: str,
    command: Mapping[str, Any],
    fleet_id: str,
    command_id: str,
    session_id: str,
    runtime_product: str,
) -> Mapping[str, Any]:
    argv = [
        *_trusted_core_command(),
        "--db",
        str(_trusted_core_db()),
        action,
        "--fleet",
        fleet_id,
        "--command-id",
        command_id,
        "--command-json",
        json.dumps(command, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        "--session-id",
        session_id,
        "--runtime-product",
        runtime_product,
    ]
    try:
        completed = subprocess.run(
            argv, check=False, capture_output=True, text=True, timeout=2
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CoreTransportError(f"指示をCoreで検証できませんでした: {exc}") from exc
    response_text = (
        completed.stdout
        if completed.returncode == 0 or completed.stdout.strip()
        else completed.stderr
    )
    try:
        document = json.loads(response_text)
    except json.JSONDecodeError as exc:
        raise CoreTransportError("Coreの応答を確認できませんでした。") from exc
    if completed.returncode != 0:
        if isinstance(document, Mapping) and document.get("ok") is False:
            detail = document.get("error") or completed.stderr.strip() or "unknown error"
            raise ActivationError(f"指示をCoreで検証できませんでした: {detail}")
        raise CoreTransportError("Coreの処理結果を確認できませんでした。")
    result = document.get("result") if isinstance(document, Mapping) else None
    if (
        not isinstance(document, Mapping)
        or document.get("ok") is not True
        or not isinstance(result, Mapping)
    ):
        raise CoreTransportError("Coreの成功応答を確認できませんでした。")
    return result


def _command_core_request_with_retry(
    action: str,
    command: Mapping[str, Any],
    fleet_id: str,
    command_id: str,
    session_id: str,
    runtime_product: str,
) -> Mapping[str, Any]:
    for attempt in range(2):
        try:
            return _command_core_request(
                action,
                command,
                fleet_id,
                command_id,
                session_id,
                runtime_product,
            )
        except CoreTransportError:
            if attempt == 1:
                raise
    raise AssertionError("unreachable")


def _prepare_command(
    command: Mapping[str, Any],
    fleet_id: str,
    command_id: str,
    session_id: str,
    runtime_product: str,
) -> Mapping[str, Any]:
    return _command_core_request_with_retry(
        "command.prepare", command, fleet_id, command_id, session_id, runtime_product
    )


def _consume_command(
    command: Mapping[str, Any],
    fleet_id: str,
    command_id: str,
    session_id: str,
    runtime_product: str,
) -> Mapping[str, Any]:
    return _command_core_request_with_retry(
        "command.consume", command, fleet_id, command_id, session_id, runtime_product
    )


def _current_context(
    fleet_id: str,
    agent_ref: str,
    session_id: str,
    runtime_product: str,
) -> Mapping[str, Any]:
    argv = [
        *_trusted_core_command(),
        "--db",
        str(_trusted_core_db()),
        "context.current",
        "--fleet",
        fleet_id,
        "--agent-ref",
        agent_ref,
        "--session-id",
        session_id,
        "--runtime-product",
        runtime_product,
    ]
    try:
        completed = subprocess.run(
            argv, check=False, capture_output=True, text=True, timeout=2
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ActivationError(f"現在の役割文脈をCoreで確認できませんでした: {exc}") from exc
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or "unknown error"
        raise ActivationError(f"現在の役割文脈をCoreで確認できませんでした: {detail}")
    try:
        document = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise ActivationError("Coreの役割文脈応答がJSONではありません。") from exc
    result = document.get("result") if isinstance(document, Mapping) else None
    if document.get("ok") is not True or not isinstance(result, Mapping):
        raise ActivationError("Coreの役割文脈応答が成功契約を満たしていません。")
    return result


def _session_failure(runtime_product: str, reason: str) -> dict[str, Any]:
    if runtime_product == "codex":
        return {"continue": False, "stopReason": reason, "systemMessage": reason}
    return {"systemMessage": reason}


def _activation_parts(
    command: Mapping[str, Any],
) -> tuple[str, str, str, str] | None:
    metadata = command.get("metadata")
    spec = command.get("spec")
    if (
        command.get("apiVersion") != "fleet.harness/v1"
        or command.get("kind") != "Command"
        or not isinstance(metadata, Mapping)
        or not isinstance(spec, Mapping)
    ):
        return None
    command_id = metadata.get("id")
    if not isinstance(command_id, str) or not command_id:
        return None
    if not isinstance(metadata.get("timestamp"), str) or not metadata.get("timestamp"):
        return None
    if spec.get("type") != "context.sync":
        return None
    target = spec.get("target")
    source = spec.get("source")
    payload = spec.get("payload")
    fleet_id = metadata.get("fleet_id")
    if (
        not isinstance(target, Mapping)
        or target.get("type") != "member"
        or not isinstance(source, Mapping)
        or source.get("type") != "member"
        or not isinstance(source.get("ref"), str)
        or not source.get("ref")
        or not isinstance(payload, Mapping)
    ):
        return None
    agent_ref = target.get("ref")
    activation_token = payload.get("activation_token")
    if not all(
        isinstance(value, str) and bool(value)
        for value in (fleet_id, agent_ref, activation_token)
    ):
        return None
    return fleet_id, agent_ref, command_id, activation_token


def _command_parts(command: Mapping[str, Any]) -> tuple[str, str, str] | None:
    metadata = command.get("metadata")
    spec = command.get("spec")
    if (
        command.get("apiVersion") != "fleet.harness/v1"
        or command.get("kind") != "Command"
        or not isinstance(metadata, Mapping)
        or not isinstance(spec, Mapping)
        or spec.get("type") not in NON_ACTIVATION_COMMAND_TYPES
    ):
        return None
    source = spec.get("source")
    target = spec.get("target")
    payload = spec.get("payload")
    fleet_id = metadata.get("fleet_id")
    command_id = metadata.get("id")
    agent_ref = target.get("ref") if isinstance(target, Mapping) else None
    if (
        not isinstance(source, Mapping)
        or source.get("type") != "member"
        or not isinstance(source.get("ref"), str)
        or not source.get("ref")
        or not isinstance(target, Mapping)
        or target.get("type") != "member"
        or not isinstance(payload, Mapping)
        or not isinstance(metadata.get("timestamp"), str)
        or not metadata.get("timestamp")
        or not all(
            isinstance(value, str) and bool(value)
            for value in (fleet_id, command_id, agent_ref)
        )
    ):
        return None
    return fleet_id, agent_ref, command_id


def _authoritative_parts(
    result: Mapping[str, Any], fleet_id: str, agent_ref: str
) -> tuple[int, Mapping[str, Any], Mapping[str, Any]] | None:
    context = result.get("context")
    control = result.get("control")
    if not isinstance(context, Mapping) or not isinstance(control, Mapping):
        return None
    agent = context.get("agent")
    fleet = context.get("fleet")
    assignments = context.get("assignments")
    reporting = context.get("reporting")
    revision = context.get("context_revision")
    if (
        context.get("fleet_id") != fleet_id
        or not isinstance(agent, Mapping)
        or agent.get("agent_ref") != agent_ref
        or not isinstance(agent.get("role_ref"), str)
        or not agent.get("role_ref")
        or not isinstance(fleet, Mapping)
        or not isinstance(fleet.get("objective"), str)
        or not fleet.get("objective")
        or not isinstance(assignments, list)
        or not isinstance(reporting, Mapping)
        or not isinstance(reporting.get("manager_ref"), str)
        or not reporting.get("manager_ref")
        or not isinstance(revision, int)
        or isinstance(revision, bool)
        or revision < 1
    ):
        return None
    return revision, context, control


def _persist_binding(
    db_path: Path,
    runtime_product: str,
    session_id: str,
    fleet_id: str,
    agent_ref: str,
    revision: int,
    context: Mapping[str, Any],
    control: Mapping[str, Any],
    *,
    activation_command_id: str | None = None,
    received_command_id: str | None = None,
) -> None:
    context_json = json.dumps(context, ensure_ascii=False, sort_keys=True)
    control_json = json.dumps(control, ensure_ascii=False, sort_keys=True)
    with _connect(db_path) as db:
        db.execute("BEGIN IMMEDIATE")
        existing = db.execute(
            "SELECT fleet_id,agent_ref,context_revision,context_json,control_json,state "
            "FROM session_context_bindings WHERE runtime_product=? AND session_id=?",
            (runtime_product, session_id),
        ).fetchone()
        if existing is not None and existing[5] == "active":
            if existing[0] != fleet_id or existing[1] != agent_ref:
                raise ActivationError(
                    "active sessionを別のfleetまたはagentへ再bindできません。"
                )
            if revision < existing[2]:
                raise ActivationError("古いcontext_revisionのactivationは受理できません。")
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
        if activation_command_id:
            db.execute(
                "DELETE FROM activation_attempts WHERE runtime_product=? AND session_id=? "
                "AND command_id=?",
                (runtime_product, session_id, activation_command_id),
            )
        if received_command_id:
            db.execute(
                "UPDATE command_receipts SET state='prepared',updated_at=CURRENT_TIMESTAMP "
                "WHERE runtime_product=? AND session_id=? AND command_id=?",
                (runtime_product, session_id, received_command_id),
            )


def _mark_command_consumed(
    db_path: Path, runtime_product: str, session_id: str, command_id: str
) -> None:
    with _connect(db_path, timeout_seconds=0.0) as db:
        changed = db.execute(
            "UPDATE command_receipts SET state='consumed',updated_at=CURRENT_TIMESTAMP "
            "WHERE runtime_product=? AND session_id=? AND command_id=? "
            "AND state='prepared'",
            (runtime_product, session_id, command_id),
        ).rowcount
        if changed != 1:
            raise ActivationError("Agent Fleet指示の受理準備が見つかりません。")


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
                        "SELECT fleet_id,agent_ref,state "
                        "FROM session_context_bindings "
                        "WHERE runtime_product=? AND session_id=?",
                        (runtime_product, session_id),
                    ).fetchone()
                    attempt = db.execute(
                        "SELECT fleet_id,agent_ref FROM activation_attempts "
                        "WHERE runtime_product=? AND session_id=? "
                        "ORDER BY updated_at DESC LIMIT 1",
                        (runtime_product, session_id),
                    ).fetchone()
            except (OSError, sqlite3.Error):
                return _block(
                    "active Agent Fleet sessionの現在の役割文脈を確認できません。"
                )
            if unbound is not None:
                return {}
            if row is None and attempt is None:
                return {}
            if row is not None and row[2] != "active":
                return _block("Agent Fleet sessionの役割文脈がCoreで確認されていません。")
            fleet_id = str(row[0] if row is not None else attempt[0])
            agent_ref = str(row[1] if row is not None else attempt[1])
            try:
                authoritative = _current_context(
                    fleet_id, agent_ref, session_id, runtime_product
                )
                parts = _authoritative_parts(authoritative, fleet_id, agent_ref)
                if parts is None:
                    raise ActivationError("Coreの役割文脈が契約を満たしていません。")
                revision, context, control = parts
                _persist_binding(
                    db_path,
                    runtime_product,
                    session_id,
                    fleet_id,
                    agent_ref,
                    revision,
                    context,
                    control,
                )
                result = _additional_context(context, control)
            except ActivationError as exc:
                return _block(str(exc))
            except (OSError, sqlite3.Error):
                return _block(
                    "Agent Fleetの役割文脈を保存できないためpromptを処理できません。"
                )
            result["hookSpecificOutput"]["hookEventName"] = "UserPromptSubmit"
            return result
        command = _decode_command(prompt)
        if command is None:
            return _block("Agent Fleet activation promptのJSONが不正です。")
        spec = command.get("spec")
        command_type = spec.get("type") if isinstance(spec, Mapping) else None
        if command_type == "context.sync":
            parts = _activation_parts(command)
            if parts is None:
                return _block("Agent Fleet activation promptの契約検証に失敗しました。")
            fleet_id, agent_ref, command_id, activation_token = parts
            try:
                with _connect(db_path) as db:
                    existing = db.execute(
                        "SELECT fleet_id,agent_ref,state FROM session_context_bindings "
                        "WHERE runtime_product=? AND session_id=?",
                        (runtime_product, session_id),
                    ).fetchone()
                    if existing is not None and existing[2] == "active" and (
                        existing[0] != fleet_id or existing[1] != agent_ref
                    ):
                        return _block(
                            "active sessionを別のfleetまたはagentへ再bindできません。"
                        )
                    db.execute(
                        "INSERT INTO activation_attempts("
                        "runtime_product,session_id,command_id,fleet_id,agent_ref) "
                        "VALUES(?,?,?,?,?) ON CONFLICT(runtime_product,session_id,command_id) "
                        "DO UPDATE SET updated_at=CURRENT_TIMESTAMP",
                        (runtime_product, session_id, command_id, fleet_id, agent_ref),
                    )
                authoritative = _consume_activation(
                    fleet_id,
                    command_id,
                    activation_token,
                    session_id,
                    runtime_product,
                )
            except ActivationError as exc:
                try:
                    with _connect(db_path) as db:
                        db.execute(
                            "DELETE FROM activation_attempts WHERE runtime_product=? "
                            "AND session_id=? AND command_id=?",
                            (runtime_product, session_id, command_id),
                        )
                except (OSError, sqlite3.Error):
                    pass
                return _block(str(exc))
            except (OSError, sqlite3.Error):
                return _block(
                    "Agent Fleetの役割文脈を保存できないためpromptを処理できません。"
                )
        else:
            command_parts = _command_parts(command)
            if command_parts is None:
                return _block("Agent Fleet指示の契約検証に失敗しました。")
            fleet_id, agent_ref, command_id = command_parts
            try:
                with _connect(db_path) as db:
                    receipt = db.execute(
                        "SELECT state FROM command_receipts WHERE runtime_product=? "
                        "AND session_id=? AND command_id=?",
                        (runtime_product, session_id, command_id),
                    ).fetchone()
                    if receipt is not None and receipt[0] == "consumed":
                        return _block("Agent Fleet指示はこのsessionで受理済みです。")
                    db.execute(
                        "INSERT INTO command_receipts("
                        "runtime_product,session_id,command_id,fleet_id,agent_ref,state) "
                        "VALUES(?,?,?,?,?,'pending') "
                        "ON CONFLICT(runtime_product,session_id,command_id) DO UPDATE SET "
                        "updated_at=CURRENT_TIMESTAMP",
                        (runtime_product, session_id, command_id, fleet_id, agent_ref),
                    )
                authoritative = _prepare_command(
                    command,
                    fleet_id,
                    command_id,
                    session_id,
                    runtime_product,
                )
                if authoritative.get("idempotent") is True:
                    return _block("Agent Fleet指示はこのsessionで受理済みです。")
            except ActivationError as exc:
                try:
                    with _connect(db_path) as db:
                        db.execute(
                            "DELETE FROM command_receipts WHERE runtime_product=? "
                            "AND session_id=? AND command_id=? AND state='pending'",
                            (runtime_product, session_id, command_id),
                        )
                except (OSError, sqlite3.Error):
                    pass
                return _block(str(exc))
            except (OSError, sqlite3.Error):
                return _block(
                    "Agent Fleet指示の受理準備を保存できないためpromptを処理できません。"
                )
        authoritative_parts = _authoritative_parts(
            authoritative, fleet_id, agent_ref
        )
        if authoritative_parts is None:
            return _block("Coreの役割文脈が契約検証に失敗しました。")
        revision, context, control = authoritative_parts
        rendered = _additional_context(context, control, command_type=command_type)
        additional_context = rendered["hookSpecificOutput"]["additionalContext"]
        if len(additional_context) > MAX_CONTEXT_CHARS:
            return _block("Agent Fleetの役割文脈が上限を超えています。")
        try:
            _persist_binding(
                db_path,
                runtime_product,
                session_id,
                fleet_id,
                agent_ref,
                revision,
                context,
                control,
                activation_command_id=(command_id if command_type == "context.sync" else None),
                received_command_id=(command_id if command_type != "context.sync" else None),
            )
        except (ActivationError, OSError, sqlite3.Error):
            return _block("Agent Fleetの役割文脈を保存できないためpromptを処理できません。")
        if command_type != "context.sync":
            try:
                confirmed = _consume_command(
                    command,
                    fleet_id,
                    command_id,
                    session_id,
                    runtime_product,
                )
                confirmed_parts = _authoritative_parts(
                    confirmed, fleet_id, agent_ref
                )
                if confirmed_parts != authoritative_parts:
                    raise ActivationError(
                        "Coreの指示確定時に役割文脈が変化しました。"
                    )
            except (ActivationError, OSError, sqlite3.Error) as exc:
                return _block(str(exc))
            try:
                _mark_command_consumed(
                    db_path, runtime_product, session_id, command_id
                )
            except (ActivationError, OSError, sqlite3.Error):
                # Coreの受領確定が正本である。補助的なローカル印の失敗を理由に、
                # すでに受領済みの指示をモデルへ渡さない状態にはしない。
                pass
        result = rendered
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
                    "SELECT fleet_id,agent_ref,state FROM session_context_bindings "
                    "WHERE runtime_product=? AND session_id=?",
                    (runtime_product, session_id),
                ).fetchone()
                attempt = db.execute(
                    "SELECT fleet_id,agent_ref FROM activation_attempts "
                    "WHERE runtime_product=? AND session_id=? "
                    "ORDER BY updated_at DESC LIMIT 1",
                    (runtime_product, session_id),
                ).fetchone()
        except (OSError, sqlite3.Error):
            return _session_failure(
                runtime_product, "Agent Fleetの役割文脈を復元できませんでした。"
            )
        if row is None and attempt is None:
            return {}
        if row is not None and row[2] == "unbound":
            return _unbound_context()
        if row is not None and row[2] != "active":
            return _session_failure(
                runtime_product, "Agent Fleet sessionの役割文脈がCoreで確認されていません。"
            )
        fleet_id = str(row[0] if row is not None else attempt[0])
        agent_ref = str(row[1] if row is not None else attempt[1])
        try:
            authoritative = _current_context(
                fleet_id, agent_ref, session_id, runtime_product
            )
            parts = _authoritative_parts(authoritative, fleet_id, agent_ref)
            if parts is None:
                raise ActivationError("Coreの役割文脈が契約を満たしていません。")
            revision, context, control = parts
            _persist_binding(
                db_path,
                runtime_product,
                session_id,
                fleet_id,
                agent_ref,
                revision,
                context,
                control,
            )
            return _additional_context(context, control)
        except (ActivationError, OSError, sqlite3.Error):
            return _session_failure(
                runtime_product, "Agent Fleetの現在の役割文脈をCoreで確認できませんでした。"
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
    event: Mapping[str, Any] | None = None
    try:
        event = json.load(sys.stdin)
        if not isinstance(event, Mapping):
            raise ValueError("hook input must be a JSON object")
        result = handle(
            event, _default_db(args.runtime_product), runtime_product=args.runtime_product
        )
    except Exception as exc:
        reason = f"Agent Fleet hookを安全に実行できませんでした: {exc}"
        result = (
            _block(reason)
            if isinstance(event, Mapping)
            and event.get("hook_event_name") == "UserPromptSubmit"
            else _session_failure(args.runtime_product, reason)
        )
    if result:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
