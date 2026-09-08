---
name: provision-herdr-fleet
description: 検査済みFleet JSONからHerdr 0.8のworkspace、tab、pane、agent編成を計画・適用し、論理エージェントへ指示を配送する。Fleet Specや役割は定義しない。
---

# provision-herdr-fleet

Herdrは実行環境と表示のAdapterであり、Fleetの正本ではない。`agent-fleet-core`が出した検査済みPortable Fleet JSONまたは版付きの指示JSONだけを入力にする。Core pluginのfileを探索・相対参照しない。

## 先に計画を確認する

詳しいCLIと復旧規則は`adapter/SKILL.md`を全文読む。入口は`adapter/scripts/fleet-herdr`で、dry-runが既定である。

利用者の統合入口は`adapter/scripts/fleet-runtime`である。Fleet YAMLの絶対パスを`plan /absolute/path/to/fleet.yml`、`start /absolute/path/to/fleet.yml --execute`へ渡す。停止・状態確認では起動結果または`runs`から得たrun IDを`stop <run-id> --execute`、`status <run-id>`へ渡す。Core CLIは`--core-command`、`AGENT_FLEET_CORE_COMMAND`、`PATH`の順で明示的に解決し、別pluginの配置を推測しない。Fleetはメンバー別の起動コマンドとモデルを同じファイルに持ち、`spec.view_profile`を省略した場合はplugin既定ViewProfileを使う。

`provision`は解決済みFleet、ViewProfile、起動条件を照合し、layout groupとweightを再現可能なsplit計画へ変換する。生成argvと論理エージェントの配置を確認してから、利用者が明示した場合だけ`--execute`を使う。実行後はFleet人数とpane数、一対一binding、pane矩形の幅・高さ・位置・重なり・空白、split数・方向・比率をHerdrの観測値で検査する。不一致ならbindingとplacementを保存せず、作成したworkspaceを閉じる。

## Bindingと配送

実行時の関連付けと表示位置はadapter専用SQLiteへ保存する。Core DBやFleet Specへpane IDを書き戻さない。マネージャーはpane IDではなく論理`agent_ref`へ指示JSONを送り、adapterが現在の関連付けを解決する。

paneが消えたら`lost`として報告し、自動再作成・自動再bindを行わない。HookがCore照合した受領は`delivered`として確定し、受領を確認できないprompt timeoutだけを`unknown`として自動retryしない。task完了はCoreへの明示`task.report`だけで確定する。

`start --execute`は起動ごとに一意なrun IDを発行し、同じFleet定義の複数runを同時に許可する。runごとにworkspace、Core DB、Herdr DB、task、command、hook contextを隔離する。YAMLの`metadata.id`はdefinition IDとしてのみ保持し、実行中の配送identityにはrun IDを使う。状態確認、停止、削除はrun IDを対象にし、controllerだけが終了したactive runは`resume <run-id>`で再開する。

実行対象はlocal Herdr 0.8に限定する。`fleet-runtime start`はpaneを持たない配送制御をforegroundで実行する。一つのrunの制御処理は一つに限定し、一時障害時は上限付きbackoffで回復する。`Ctrl-C`は配送制御だけを終了し、`fleet-runtime stop <run-id>`は対象runのworkspaceも閉じる。daemon、multi-host、fleet間gateway、独自Web UIは対象外である。
