---
name: control-agent-fleet
description: YAML Fleet Specを検査し、論理エージェント、タスク、型付き指示、出来事をSQLiteで管理する。Herdr paneの作成やUI配置には使わない。
---

# control-agent-fleet

このskillを読み終えたagentは、Fleet Specを検査済みJSONにし、Core CLIでlogical fleetを初期化・割当・報告・受理・観測できる。Fleet Specはdesired state、SQLiteは実行時のlogical stateとして分ける。pane ID、workspace ID、tab ID、観測geometryはFleetにもCore DBにも保存しない。

## 入力

- Fleet YAMLの絶対path。`fleet.harness/v3`で、各memberに役割と分離した`runtime.product`、`runtime.command`、`runtime.model`、`runtime.effort`、`runtime.fallback`を持つ。`spec.view_profile`は任意のViewProfile pathで、省略時はHerdr pluginの既定値を使う。
- `agent-roles`が書き出した検査済みRole Catalog JSONの絶対path。役割の本文はFleet内に定義せず、このCatalogから解決する。
- Core DBの絶対path（`--db`）。

## 判断基準

| 観察対象 | 判定 |
|---|---|
| provisionできるか | `validate_fleet.py`が終了code 0でnormalized JSONを返したときだけ。未知field、重複member／task、参照切れ、task依存cycle、Catalogに無い`role_ref`、managerの権限不足はどれも修正が先 |
| task.assignできるか | 全`depends_on`が`accepted`のときだけ。未受理があればCoreがIDと状態を示して拒否する |
| 完了の判定 | 成果物をmanagerの受理待ちへ渡すstatusは`reported`。managerが根拠を確認した後だけ`task.accept`で`accepted`。paneの出力、待機表示、完了表示は完了の証拠にならない |
| 状態の読み取り口 | managerは`task.list`を正本とする。SQLiteの直接参照や外部JSON加工commandは使わない |
| 配送の確定 | Hookが指示内容・宛先・受信sessionをCoreと照合した時点で配送済み。入力送信の時間切れで配送状態が不明な指示は自動再送しない |
| Adapterへ渡すもの | 版付きの公開JSON契約だけ。相手pluginの導入先を探索・importしない |

## 手順

### 1. Fleet Specを検査する

package共有のtool`../../spec/scripts/validate_fleet.py`で検査する。pathはこのSKILL.mdがある入口directoryを基準にした相対pathである。

| 呼び出し | 入力 | 出力 | 失敗の観測 | 失敗時 |
|---|---|---|---|---|
| `python3 ../../spec/scripts/validate_fleet.py <fleet.ymlの絶対path> --role-catalog <catalog.jsonの絶対path> --output-json` | Fleet YAMLとRole Catalog | stdoutにnormalized Fleet JSON | 終了code非0、stderrに違反箇所 | 違反を直すまでprovisionしない |

### 2. Coreを操作する

CLIは`../../core/scripts/fleet-control --db <Core DBの絶対path> <操作>`である。状態遷移と各操作の契約は[Core CLIの契約](references/fleet-control-runtime.md)を全文読む。

| 操作 | 役割 |
|---|---|
| `fleet.provision --config <fleet.yml> --role-catalog <catalog.json>` | 検査済みSpecからlogical fleetを初期化する。同じFleet IDと同じ解決結果は冪等成功、内容が違えば拒否 |
| `task.assign` | managerがtaskをlogical agentへ割り当てる |
| `message.send` | managerが論理エージェント宛ての型付き指示を配送待ちへ積む |
| `task.report --agent-ref <self> --status running\|blocked\|reported\|failed` | 割当済みagentが状態を明示報告する。終端報告には検証結果または停止理由を含める |
| `task.accept` | managerだけが`reported`を`accepted`にする |
| `task.list --fleet <fleet_id>` | 読み取り専用。タスク状態、宣言順の`depends_on`、最新報告を返す |
| `fleet.reconcile` | current state、event、pending outboxを返す |

各操作はJSON `{"ok": true, "result": ...}`をstdoutへ返し、拒否時は`{"ok": false, "error": ...}`と終了code非0を返す。拒否理由を読み、状態を変えずに止まる。

## 停止条件

- Fleet Specの検査が失敗した。
- `task.assign`が未受理の依存を示して拒否した。
- 配送状態が`unknown`のまま受領を確認できない（再送せず、managerへ報告する）。
- 実行環境や表示の操作が必要になった。`agent-fleet-herdr`を別途使い、Fleet YAMLの絶対pathを統合入口へ渡す。

## 出力

検査済みFleet JSON、初期化されたCore DB、task状態の推移、配送待ち指示。終端報告は同じ処理でmanager宛て通知になる。
