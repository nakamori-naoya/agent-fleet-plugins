"""Transactional command delivery leases; no provider or UI knowledge."""
from __future__ import annotations
import uuid
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any
from core_contract import FleetError, utc_now


class CommandDelivery:
    @staticmethod
    def _parse_timestamp(value: str, label: str) -> datetime:
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as exc:
            raise FleetError(f"{label} must be an ISO 8601 timestamp") from exc
        if parsed.tzinfo is None:
            raise FleetError(f"{label} must include a timezone")
        return parsed


    def _command_document(
        self, db: sqlite3.Connection, row: sqlite3.Row
    ) -> dict[str, Any]:
        return {
            "apiVersion": "fleet.harness/v1",
            "kind": "Command",
            "metadata": {
                "id": row["command_id"],
                "fleet_id": row["fleet_id"],
                "timestamp": row["created_at"],
            },
            "spec": {
                "source": {"type": "member", "ref": row["sender_ref"]},
                "target": {"type": "member", "ref": row["target_agent_ref"]},
                "type": row["command_type"],
                "payload": json.loads(row["payload_json"]),
                "context": self._context_capsule(
                    db, row["fleet_id"], row["target_agent_ref"]
                ),
            },
        }


    def claim_delivery(
        self,
        fleet_id: str,
        delivery_worker_id: str,
        now: str | None = None,
        lease_seconds: int = 30,
    ) -> dict[str, Any] | None:
        if not delivery_worker_id.strip():
            raise FleetError("delivery_worker_id is required")
        if lease_seconds <= 0:
            raise FleetError("lease_seconds must be positive")
        now_at = self._parse_timestamp(now or utc_now(), "now")
        now_value = now_at.astimezone(timezone.utc).isoformat(timespec="milliseconds")
        lease_expires_at = (now_at + timedelta(seconds=lease_seconds)).isoformat(
            timespec="milliseconds"
        )
        lease_token = str(uuid.uuid4())
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            expired_sending = list(
                db.execute(
                    "SELECT command_id FROM outbox WHERE fleet_id=? AND status='sending' "
                    "AND lease_expires_at<=?",
                    (fleet_id, now_value),
                )
            )
            db.execute(
                "UPDATE outbox SET status='unknown',result_detail=?,lease_owner=NULL,"
                "lease_token=NULL,lease_expires_at=NULL WHERE fleet_id=? AND status='sending' "
                "AND lease_expires_at<=?",
                ("delivery process ended after external send began", fleet_id, now_value),
            )
            for expired in expired_sending:
                self._append_event(
                    db,
                    fleet_id,
                    "system",
                    expired["command_id"],
                    "delivery.unknown",
                    {"detail": "delivery lease expired after external send began"},
                )
            db.execute(
                "UPDATE outbox SET status='pending',lease_owner=NULL,lease_token=NULL,"
                "lease_expires_at=NULL WHERE fleet_id=? AND status='processing' "
                "AND lease_expires_at<=?",
                (fleet_id, now_value),
            )
            row = db.execute(
                "SELECT command_id,fleet_id,sender_ref,target_agent_ref,command_type,payload_json,"
                "created_at,attempt_count FROM outbox WHERE fleet_id=? AND "
                "(status='pending' OR (status='retry' AND next_attempt_at<=?)) "
                "AND (command_type='context.sync' OR EXISTS ("
                "SELECT 1 FROM member_context_state c WHERE c.fleet_id=outbox.fleet_id "
                "AND c.agent_ref=outbox.target_agent_ref "
                "AND c.confirmed_revision=c.context_revision)) "
                "ORDER BY CASE command_type "
                "WHEN 'task.report' THEN 0 WHEN 'context.sync' THEN 1 ELSE 2 END,"
                "created_at,command_id LIMIT 1",
                (fleet_id, now_value),
            ).fetchone()
            if row is None:
                return None
            updated = db.execute(
                "UPDATE outbox SET status='processing',lease_owner=?,lease_token=?,"
                "lease_expires_at=?,attempt_count=attempt_count+1 WHERE fleet_id=? "
                "AND command_id=? AND status IN ('pending','retry')",
                (
                    delivery_worker_id,
                    lease_token,
                    lease_expires_at,
                    fleet_id,
                    row["command_id"],
                ),
            )
            if updated.rowcount != 1:
                raise FleetError("delivery command was claimed by another worker")
            self._append_event(
                db,
                fleet_id,
                "system",
                row["command_id"],
                "delivery.claimed",
                {
                    "delivery_worker_id": delivery_worker_id,
                    "lease_expires_at": lease_expires_at,
                    "attempt_count": int(row["attempt_count"]) + 1,
                },
            )
            command_document = self._command_document(db, row)
        return {
            "command": command_document,
            "delivery": {
                "status": "processing",
                "lease_owner": delivery_worker_id,
                "lease_token": lease_token,
                "lease_expires_at": lease_expires_at,
                "attempt_count": int(row["attempt_count"]) + 1,
            },
        }


    def begin_delivery(
        self,
        fleet_id: str,
        command_id: str,
        lease_token: str,
        now: str | None = None,
    ) -> dict[str, Any]:
        """Fence the point immediately before the external Herdr send begins."""

        with self.connect() as db:
            command = db.execute(
                "SELECT status,lease_token,lease_expires_at,attempt_count FROM outbox "
                "WHERE fleet_id=? AND command_id=?",
                (fleet_id, command_id),
            ).fetchone()
            if command is None:
                raise FleetError(f"unknown command: {command_id}")
            if command["status"] != "processing" or command["lease_token"] != lease_token:
                raise FleetError("delivery lease token is stale or invalid")
            current_time = self._parse_timestamp(now or utc_now(), "now")
            expires_at = self._parse_timestamp(command["lease_expires_at"], "lease_expires_at")
            if expires_at <= current_time:
                raise FleetError("delivery lease has expired")
            db.execute(
                "UPDATE outbox SET status='sending' WHERE fleet_id=? AND command_id=?",
                (fleet_id, command_id),
            )
            self._append_event(
                db,
                fleet_id,
                "system",
                command_id,
                "delivery.started",
                {"attempt_count": command["attempt_count"]},
            )
        return {
            "command_id": command_id,
            "status": "sending",
            "attempt_count": command["attempt_count"],
        }


    def record_delivery_result(
        self,
        fleet_id: str,
        command_id: str,
        lease_token: str,
        result: str,
        detail: str | None = None,
        now: str | None = None,
    ) -> dict[str, Any]:
        if result not in {"delivered", "unknown", "retry", "abandoned"}:
            raise FleetError(f"unsupported delivery result: {result}")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            command = db.execute(
                "SELECT status,lease_token,lease_expires_at,attempt_count,"
                "activation_consumed_at FROM outbox "
                "WHERE fleet_id=? AND command_id=?",
                (fleet_id, command_id),
            ).fetchone()
            if command is None:
                raise FleetError(f"unknown command: {command_id}")
            if command["status"] == "delivered" and command["activation_consumed_at"]:
                return {
                    "command_id": command_id,
                    "status": "delivered",
                    "attempt_count": command["attempt_count"],
                    "idempotent": True,
                }
            if command["status"] == "processing" and command["lease_token"] == lease_token:
                raise FleetError("delivery has not started")
            if command["status"] != "sending" or command["lease_token"] != lease_token:
                raise FleetError("delivery lease token is stale or invalid")
            current_time = self._parse_timestamp(now or utc_now(), "now")
            completed_at = current_time.astimezone(timezone.utc).isoformat(
                timespec="milliseconds"
            )
            expires_at = self._parse_timestamp(command["lease_expires_at"], "lease_expires_at")
            if expires_at <= current_time:
                raise FleetError("delivery lease has expired")
            if result == "delivered" and not command["activation_consumed_at"]:
                raise FleetError(
                    "delivery cannot be confirmed without an Agent Fleet hook receipt"
                )
            next_attempt_at = completed_at if result == "retry" else None
            db.execute(
                "UPDATE outbox SET status=?,result_detail=?,delivered_at=?,next_attempt_at=?,"
                "lease_owner=NULL,lease_token=NULL,lease_expires_at=NULL "
                "WHERE fleet_id=? AND command_id=?",
                (
                    result,
                    detail,
                    completed_at if result == "delivered" else None,
                    next_attempt_at,
                    fleet_id,
                    command_id,
                ),
            )
            self._append_event(
                db,
                fleet_id,
                "system",
                command_id,
                f"delivery.{result}",
                {"detail": detail, "attempt_count": command["attempt_count"]},
            )
        return {
            "command_id": command_id,
            "status": result,
            "attempt_count": command["attempt_count"],
        }

