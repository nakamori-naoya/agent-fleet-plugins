---
name: fleet-control-runtime
description: SQLiteを正本としてfleet、logical agent、task、event、typed command outboxをローカル管理する。
---

# Fleet control runtime

`scripts/fleet-control --db <path> fleet.provision --config fleet.yml` で初期化する。Coreは `../spec/scripts/validate_fleet.py <fleet.yml> --output-json` をsubprocess実行し、検査済みnormalized JSONだけをDBへ反映する。未検査YAMLのfallback parseはしない。MVPの公開コマンドは `fleet.provision`、`task.assign`、`message.send`、`task.report`、`fleet.reconcile` である。

memberは再利用可能な役割を `role_ref` で参照する。taskは `pending -> assigned -> running -> blocked|completed|failed` の順に更新し、`blocked -> running` だけ再開として許可する。その他の逆行やterminal状態からの更新は拒否する。割当済みagentだけが`task.report --agent-ref <self>`を実行でき、完了・失敗・blockedは`--report '{...}'`による明示reportを必要とする。

managerだけがlogical `agent_ref` 宛てに `fleet.provision`、`task.assign`、`message.send`、`task.report`、`fleet.reconcile` をoutboxへ積める。pane IDなどadapter固有の識別子をCore DBへ保存しない。

daemon、multi-host、fleet gateway、独自TUIは対象外である。
