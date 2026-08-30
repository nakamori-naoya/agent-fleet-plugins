---
name: control-agent-fleet
description: YAML Fleet Specを検査し、logical agent、task、typed command、eventをSQLiteで管理する。Herdr paneの作成やUI配置には使わない。
---

# control-agent-fleet

Fleet Specはdesired state、SQLiteは実行時のlogical stateとして分ける。pane ID、workspace ID、tab ID、UI geometryをCore DBへ保存しない。

## Fleet Specを検査する

```bash
PLUGIN_ROOT="${CLAUDE_PLUGIN_ROOT:-/absolute/path/to/agent-fleet-core}"
python3 "${PLUGIN_ROOT}/spec/scripts/validate_fleet.py" fleet.yml --output-json
```

成功したnormalized JSONだけをCoreへ渡す。未知field、重複member/task、参照切れ、task依存cycle、manager/advisorのrole不一致を修正するまでprovisionしない。

## Coreを操作する

詳しい状態遷移とCLI契約は`core/SKILL.md`を全文読む。入口は`core/scripts/fleet-control`である。

- `fleet.provision`: 検査済みSpecからlogical fleetを初期化する
- `task.assign`: managerがtaskをlogical agentへ割り当てる
- `message.send`: managerがlogical agent宛てのtyped commandをoutboxへ積む
- `task.report`: 割当済みagentが`--agent-ref`を示し、running、blocked、completed、failedを明示報告する
- `fleet.reconcile`: current state、event、pending outboxを返す

task完了をpane output、idle、doneから推測しない。terminal reportには検証結果またはblocked理由を含める。prompt timeoutで配送状態が不明なcommandを自動再送しない。

## Adapterとの境界

Herdr Adapterへは`fleet.harness/v1`のCommand envelopeだけを渡す。相手pluginのinstall pathを探索・importしない。Runtime/View操作が必要なら`agent-fleet-herdr`を別途使う。
