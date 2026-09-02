---
name: fleet-control-runtime
description: SQLiteを正本としてfleet、logical agent、task、event、typed command outboxをローカル管理する。
---

# Fleet control runtime

`scripts/fleet-control --db <path> fleet.provision --config fleet.yml --role-catalog <catalog.json>` で初期化する。Coreは `../spec/scripts/validate_fleet.py <fleet.yml> --role-catalog <catalog.json> --output-json` をsubprocess実行し、Role Catalogから役割定義を解決したnormalized JSONだけをDBへ反映する。未検査YAMLのfallback parseはしない。同じFleet IDと同じ解決結果の再初期化は冪等成功とし、同じIDでFleetまたはRole Catalogの内容が違う場合は拒否する。`spec.validate`は状態を変更せず検査済みFleet JSONを返す。

memberは再利用可能な役割を `role_ref` で参照する。taskは `pending -> assigned -> running -> blocked|reported|failed` と進み、マネージャーの`task.accept`だけが`reported -> accepted`を成立させる。`blocked -> running`と、差し戻しによる`reported -> running`を許可し、その他の逆行や終端状態からの更新は拒否する。割当済みagentだけが`task.report --agent-ref <self>`を実行でき、完了・失敗・blockedは明示reportを必要とする。`assigned`、`blocked`、`reported`から`running`への移行は目的、役割、担当、完了条件を変えないため役割文脈を改訂せず、作業中のagentへ文脈同期を割り込ませない。終端報告はマネージャー宛て指示として同じtransactionで保存する。

マネージャーの進捗監視には`scripts/fleet-control --db <path> task.list --fleet <fleet_id>`を使う。この読み取り専用操作は、タスクの担当・状態・期待する成果・完了条件・最新の状態報告・最新の進捗報告・次回報告予定・報告期限状態だけを小さいJSONとして返す。状態報告はタスク受入後も残る。マネージャーはSQLiteを直接読まず、`status`の全状態を外部のJSON加工commandで絞り込まない。`reported`、完了条件、状態報告をこの操作だけで照合した後に`task.accept`する。

managerだけがlogical `agent_ref` 宛てに型付きの指示を配送待ちへ積める。各指示には、対象agentの目的、役割、担当、完了条件、停止条件、報告先、役割文脈改訂番号を含める。現在の改訂がHookで確認されるまで、役割文脈同期以外を配送しない。通常指示もHookがCore保存内容、宛先、受信sessionを照合し、その受領時点を配送済みとする。期限付き確保後、受領を確認できない場合は自動再送しない。pane IDなどadapter固有の識別子をCore DBへ保存しない。

daemon、multi-host、fleet gateway、独自TUIは対象外である。
