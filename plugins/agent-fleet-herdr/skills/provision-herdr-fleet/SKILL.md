---
name: provision-herdr-fleet
description: 検査済みFleet JSONからHerdr 0.8のworkspace、tab、pane、agent編成を計画・適用し、論理エージェントへ指示を配送する。Fleet Specや役割は定義しない。
---

# provision-herdr-fleet

このskillを読み終えたagentは、Fleet YAMLからHerdr上の艦隊をdry-runで計画し、利用者が明示したときだけ起動し、runごとに状態確認・停止・再開・削除ができる。Herdrは実行環境と表示のAdapterであり、Fleet状態の一次データではない。入力は`agent-fleet-core`が出した検査済みPortable Fleet JSONまたは版付きの指示JSONだけで、Core pluginのfileは探索・相対参照しない。

## 入力

- Fleet YAMLの絶対path。各memberの起動コマンドとモデルを同じfileに持つ。`spec.view_profile`を記載する場合はViewProfileの絶対pathを使い、相対path、`~`、環境変数による省略は使わない。省略した場合はplugin既定ViewProfileを使う。
- Role Catalog JSON。`--role-catalog`または`AGENT_ROLES_CATALOG`で明示する。
- Core CLI。`--core-command`、`AGENT_FLEET_CORE_COMMAND`、`PATH`の順で明示的に解決する。別pluginの配置は推測しない。
- 操作対象のrun ID（状態確認・停止・再開・削除のとき）。起動結果または`runs`から得る。

## 判断基準

| 観察対象 | 判定 |
|---|---|
| 実行するか | `--execute`は利用者が実行を明示した場合だけ付ける。無ければdry-run |
| provisionが成立したか | Fleet人数とpane数、一対一binding、pane矩形の幅・高さ・位置・重なり・空白、split数・方向・比率がHerdrの観測値と一致したときだけ。一つでも不一致ならbindingとplacementを保存せず、作成したworkspaceを閉じる |
| 配送の確定 | HookがCore照合した受領は`delivered`。受領を確認できないprompt timeoutは`unknown`で自動retryしない |
| paneが消えたとき | `lost`として報告する。自動再作成・自動再bindはしない |
| task完了 | Coreへの明示`task.report`だけで確定する |
| runの同一性 | `start --execute`ごとに一意なrun IDを発行する。YAMLの`metadata.id`はdefinition IDであり、実行操作にはrun IDを使う |

## 手順

CLIは`../../adapter/scripts/fleet-runtime`（利用者向け統合入口）と`../../adapter/scripts/fleet-herdr`（Adapter CLI、dry-run既定）である。pathはこのSKILL.mdがある入口directoryを基準にした相対pathである。CLIの契約と復旧規則は[Adapter CLIの安全契約](references/fleet-herdr-runtime.md)を全文読む。

| 呼び出し | 効果 | 出力 | 失敗の観測 | 失敗時（止まるか回復するか） |
|---|---|---|---|---|
| `../../adapter/scripts/fleet-runtime plan <fleet.ymlの絶対path>` | 艦隊、表示、起動コマンド、pane計画を返す。DBもdirectoryも作らない | stdoutにJSON（`ok`, `result`） | 終了code非0、`ok: false`と`error` | 止まる。入力を直してから再実行する |
| `../../adapter/scripts/fleet-runtime start <fleet.ymlの絶対path> --execute` | 新しいrunとしてCore、Herdr、役割文脈、初期タスクを準備し、配送制御をforegroundで続ける | run IDを含むJSON | 同上。起動前検査（fleet-controller、Core、Herdr adapter、Hook source）の不一致は起動しない | 起動前検査の不一致とprovision後の観測不一致は止まる（bindingを保存せずworkspaceを閉じる）。pane作成時のHerdr `agent_pane_busy`だけは1.0秒間隔で最大3回まで再試行し、超えたら止まる。配送制御の一時障害（Core / Herdr CLIの失敗）は`poll_seconds × 2^n`（上限5秒）のbackoffで回復を試み、成功したら回数を戻す。一つのrunの制御処理は一つに限定し、既にcontrollerがあるrunでは`already has a controller`で止まる |
| `../../adapter/scripts/fleet-runtime status <run-id>` | CoreとHerdrの公開CLIで対象runの状態を結合する | JSON | 同上 | 止まる（読み取り専用。再試行しない） |
| `../../adapter/scripts/fleet-runtime stop <run-id> --execute` | 配送制御を終え、対象runのworkspaceも閉じる | JSON | 同上 | 止まる。既に停止済みなら`already_stopped`を冪等に返す |
| `../../adapter/scripts/fleet-runtime resume <run-id>` | controllerだけが終了したactive runを再開する | JSON | 同上 | 同`start`の配送制御と同じbackoffで回復する。既にcontrollerがあるrunでは止まる |
| `../../adapter/scripts/fleet-runtime remove <run-id> --execute` | 保存したrun状態を削除する | JSON | 同上 | 止まる |

配送そのものは再試行しない。受領を確認できないprompt timeoutは`unknown`のまま報告し、自動再送しない。

1. `plan`で生成argvと論理エージェントの配置を確認する。
2. 利用者が実行を明示したときだけ`start --execute`を使う。実行後はprovisionの成立を判断基準で確かめる。
3. bindingと表示位置はadapter専用SQLite（`--state-db`）に保存する。Core DBやFleet Specへpane IDを書き戻さない。managerはpane IDではなく論理`agent_ref`へ指示JSONを送り、adapterが現在の関連付けを解決する。
4. `Ctrl-C`は配送制御だけを終了する。workspaceも閉じるなら`stop <run-id> --execute`を使う。

## 停止条件

止まるのは、安全な状態を保てないか、契約外の対象に触れるときである。

- 起動前検査で不一致があった（state作成前に止まる）。
- provision後の観測値がFleetと一致しない（bindingを保存せずworkspaceを閉じて報告する）。
- paneが`lost`になった（`bind`または`rebind`で明示的に修復するまで進めない）。
- 実行を明示されていないのに`--execute`が要る操作に到達した。dry-runの結果を示し、実行の指示を待つ。
- 対象がlocal Herdr 0.8以外である。daemon、multi-host、fleet間gateway、独自Web UIは対象外。

止まるときは、実行した操作、CLIの`error`、閉じたworkspace、保存しなかった状態、修復または再実行に必要な操作を返す。

判断の揺れでは止まらない。ViewProfileの配置やpane計画の良し悪しに疑問があっても、`plan`が返す計画を仮説として示して利用者に判断を委ね、計画を勝手に変えない。配送状態が`unknown`のときは失敗とも成功とも決めず、`unknown`のまま報告する。

## 出力

計画JSON、run ID、runごとのworkspace・Core DB・Herdr DB・task・command・hook contextの隔離された状態、配送結果（`delivered` / `unknown` / `lost`）。
