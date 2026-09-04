---
name: provision-herdr-fleet
description: 検査済みFleet JSONからHerdr 0.8のworkspace、tab、pane、agent編成を計画・適用し、論理エージェントへ指示を配送する。Fleet Specや役割は定義しない。
---

# provision-herdr-fleet

Herdrは実行環境と表示のAdapterであり、Fleetの正本ではない。`agent-fleet-core`が出した検査済みPortable Fleet JSONまたは版付きの指示JSONだけを入力にする。Core pluginのfileを探索・相対参照しない。

## 先に計画を確認する

詳しいCLIと復旧規則は`adapter/SKILL.md`を全文読む。入口は`adapter/scripts/fleet-herdr`で、dry-runが既定である。

利用者が複数の起動設定から選ぶ入口は`adapter/scripts/fleet-runtime`である。最初に`init`と`doctor`を実行し、`list`、`plan <launch_id>`、`start <launch_id> --execute`、`status <launch_id>`を使う。停止は`stop <launch_id> --execute`、設定を作り直す場合だけ`remove <launch_id> --execute`を使う。Core CLIは`--core-command`、`AGENT_FLEET_CORE_COMMAND`、`PATH`の順で明示的に解決し、別pluginの配置を推測しない。Herdr LaunchProfileだけが`fleet_ref`と版固定`view_profile_ref`を一方向参照する。

`provision`は別入力のPortable Fleet、Herdr LaunchProfile、ViewProfileを照合し、layout groupとweightを再現可能なsplit計画へ変換する。FleetとViewProfileを互いに参照させない。艦隊編成、起動設定、pane比率の実体をplugin内へ同梱せず、利用者の設定directoryからだけ読む。生成argvと論理エージェントの配置を確認してから、利用者が明示した場合だけ`--execute`を使う。

## Bindingと配送

実行時の関連付けと表示位置はadapter専用SQLiteへ保存する。Core DBやFleet Specへpane IDを書き戻さない。マネージャーはpane IDではなく論理`agent_ref`へ指示JSONを送り、adapterが現在の関連付けを解決する。

paneが消えたら`lost`として報告し、自動再作成・自動再bindを行わない。HookがCore照合した受領は`delivered`として確定し、受領を確認できないprompt timeoutだけを`unknown`として自動retryしない。task完了はCoreへの明示`task.report`だけで確定する。

実行対象はlocal Herdr 0.8に限定する。`fleet-runtime start`はpaneを持たない配送制御をforegroundで実行する。同じFleetの制御処理は一つに限定し、一時障害時は上限付きbackoffで回復する。`Ctrl-C`は配送制御だけを終了し、`fleet-runtime stop`はworkspaceも閉じる。daemon、multi-host、fleet間gateway、独自Web UIは対象外である。
