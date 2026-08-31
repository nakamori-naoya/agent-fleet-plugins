---
name: fleet-control-runtime
description: SQLiteを正本としてfleet、logical agent、task、event、typed command outboxをローカル管理する。
---

# Fleet control runtime

`scripts/fleet-control --db <path> fleet.provision --config fleet.yml` で初期化する。Coreは `../spec/scripts/validate_fleet.py <fleet.yml> --output-json` をsubprocess実行し、検査済みnormalized JSONだけをDBへ反映する。未検査YAMLのfallback parseはしない。同じFleet IDと同じ内容の再初期化は冪等成功とし、同じIDで内容が違う場合は拒否する。`spec.validate`は状態を変更せず検査済みFleet JSONを返す。

memberは再利用可能な役割を `role_ref` で参照する。taskは `pending -> assigned -> running -> blocked|reported|failed` と進み、マネージャーの`task.accept`だけが`reported -> accepted`を成立させる。`blocked -> running`と、差し戻しによる`reported -> running`を許可し、その他の逆行や終端状態からの更新は拒否する。割当済みagentだけが`task.report --agent-ref <self>`を実行でき、完了・失敗・blockedは明示reportを必要とする。

managerだけがlogical `agent_ref` 宛てに型付きの指示を配送待ちへ積める。各指示には、対象agentの目的、役割、担当、完了条件、停止条件、報告先、役割文脈改訂番号を含める。`context.confirm`で現在の改訂が確認されるまで、役割文脈同期以外を配送しない。期限付き確保後、外部送信の直前を記録し、送信開始後に結果を確認できない場合は自動再送しない。pane IDなどadapter固有の識別子をCore DBへ保存しない。

daemon、multi-host、fleet gateway、独自TUIは対象外である。
