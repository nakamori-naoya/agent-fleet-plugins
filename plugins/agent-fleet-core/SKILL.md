---
name: control-agent-fleet
description: YAML Fleet Specを検査し、論理エージェント、タスク、型付き指示、出来事をSQLiteで管理する。Herdr paneの作成やUI配置には使わない。
---

# control-agent-fleet

Fleet Specはdesired state、SQLiteは実行時のlogical stateとして分ける。viewは版固定`profile_ref`だけを持ち、Coreはその形式だけを検査する。pane ID、workspace ID、tab ID、比率、UI geometryをCore DBへ保存しない。

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
- `message.send`: マネージャーが論理エージェント宛ての型付き指示を配送待ちへ積む
- `task.report`: 割当済みagentが`--agent-ref`を示し、running、blocked、completed、failedを明示報告する
- `fleet.reconcile`: current state、event、pending outboxを返す

タスク完了をpaneの出力、待機表示、完了表示から推測しない。終端報告には検証結果または停止理由を含める。入力送信の時間切れで配送状態が不明な指示を自動再送しない。

## Adapterとの境界

Herdr Adapterへは`fleet.harness/v1`の指示JSONだけを渡す。相手pluginの導入先を探索・importしない。実行環境や表示の操作が必要なら`agent-fleet-herdr`を別途使う。
