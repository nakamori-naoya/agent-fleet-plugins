---
name: control-agent-fleet
description: YAML Fleet Specを検査し、論理エージェント、タスク、型付き指示、出来事をSQLiteで管理する。Herdr paneの作成やUI配置には使わない。
---

# control-agent-fleet

Fleet Specはdesired state、SQLiteは実行時のlogical stateとして分ける。各メンバーには、役割と分離した`runtime.product`、`runtime.model`、`runtime.effort`、`runtime.fallback`を必ず指定する。Coreだけで使うFleetはspec直下のHerdr用`runtime`と`view`を省略できる。Herdr Adapterを使う場合だけ版固定`profile_ref`を指定する。pane ID、workspace ID、tab ID、比率、UI geometryをCore DBへ保存しない。

## Fleet Specを検査する

```bash
PLUGIN_ROOT="${CLAUDE_PLUGIN_ROOT:-/absolute/path/to/agent-fleet-core}"
python3 "${PLUGIN_ROOT}/spec/scripts/validate_fleet.py" fleet.yml \
  --role-catalog /absolute/path/to/builtin@1.json --output-json
```

成功したnormalized JSONだけをCoreへ渡す。未知field、重複member/task、参照切れ、task依存cycle、Role Catalogに存在しない`role_ref`、マネージャーに必要な権限不足を修正するまでprovisionしない。役割の本文はFleet内に定義せず、`agent-roles`が書き出した検査済みCatalogから解決する。

## Coreを操作する

詳しい状態遷移とCLI契約は`core/SKILL.md`を全文読む。入口は`core/scripts/fleet-control`である。

- `fleet.provision`: 検査済みSpecからlogical fleetを初期化する
- `task.assign`: managerがtaskをlogical agentへ割り当てる。全`depends_on`が受理済みでなければ、未受理IDと状態を示して拒否する
- `message.send`: マネージャーが論理エージェント宛ての型付き指示を配送待ちへ積む
- `task.report`: 割当済みagentが`--agent-ref`を示し、running、blocked、completed、failedを明示報告する
- `task.list`: マネージャーの監視に必要なタスク状態、宣言順の`depends_on`配列、最新報告を読み取り専用で返す
- `fleet.reconcile`: current state、event、pending outboxを返す

タスク完了をpaneの出力、待機表示、完了表示から推測しない。マネージャーは`task.list`を正本の読み取り口とし、SQLiteの直接参照や外部JSON加工commandへ依存しない。終端報告には検証結果または停止理由を含める。終端報告は同じ処理でマネージャー宛て通知にし、マネージャーだけが`task.accept`で受理する。入力送信の時間切れで配送状態が不明な指示を自動再送しない。Hookが指示内容・宛先・受信sessionをCoreと照合した時点を配送済みとする。

## Adapterとの境界

Herdr Adapterへは`fleet.harness/v1`の指示JSONだけを渡す。相手pluginの導入先を探索・importしない。実行環境や表示の操作が必要なら`agent-fleet-herdr`を別途使う。
