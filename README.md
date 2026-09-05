# Agent Fleet Plugins

YAMLのFleet Specを正本に、logical agent、task、command、eventを管理し、Herdr 0.8へruntime/view操作を委譲するClaude Code/Codex両対応marketplaceである。

公開インストール単位は`agent-fleet-core`と`agent-fleet-herdr`である。Fleetは実行基盤なので、利用者の権限を最小にするためCoreとHerdr Adapterだけは分ける。役割HookはHerdr package内の実装であり、個別のmarketplace entryやインストール対象にはしない。Coreだけを使う利用者へHerdr操作権限を同梱せず、通常セッションで役割文脈を注入しない。Fleet Specにはdesired stateだけを記載し、pane IDや実行状態はSQLiteのobserved stateへ分離する。

## こんなときに使う

**複数のAIエージェントへ役割と仕事を割り当て、報告とレビューを追跡したいときに使う。** 単に複数のCLIを並べるのではなく、誰が何を担当し、どの報告を受けて完了とするかを論理的に管理する。

- manager、worker、advisor、reviewerを決めて一つの開発目標へ取り組ませたい
- workerの自己申告だけで完了にせず、reviewerの確認後にmanagerが受け入れたい
- CodexとClaude Codeを同じ艦隊へ混在させたい
- AIアカウント、モデル、思考量、pane配置を利用者設定として切り替えたい
- Herdrを起動せず、タスク、指示、報告、Outboxだけを管理したい

一人のエージェントで完了する作業には向かない。複数ホストの常駐制御や艦隊間通信も現在の対象外である。

## どれを入れるか

| やりたいこと | 必要なplugin |
|---|---|
| Fleet Specとタスク状態だけを管理する | `agent-fleet-core` |
| Herdr上へpaneを作り、エージェントを起動する | `agent-fleet-core`と`agent-fleet-herdr` |
| 艦隊セッションだけへ役割Hookを渡す | `agent-fleet-herdr`が内包するHookを艦隊起動時だけ有効にする |

役割の意味は`agent-roles`、実行するAI製品とモデルはFleet Spec、画面配置はViewProfileがそれぞれ所有する。これらを一つの設定へ混ぜないことで、艦隊編成を変えずに表示方法やアカウントを交換できる。

## 利用の流れ

1. `agent-roles`から検査済みRole Catalogを書き出す。
2. Fleet Specへメンバー、役割、モデルを記載する。
3. AgentCommandProfile、Herdr LaunchProfile、ViewProfileを利用者領域へ置く。
4. `fleet-runtime plan <launch_id>`で解決結果を確認する。
5. `fleet-runtime start <launch_id> --execute`で艦隊を起動する。
6. taskの報告、レビュー、managerの受け入れを状態として追跡する。

たとえば、次のように依頼できる。

```text
manager 1人、worker 2人、advisor 1人、reviewer 1人の艦隊をdry-runで検査して。
```

```text
development-squadを起動し、workerの報告後にreviewerが確認するところまで進捗を追って。
```

## MVP境界

- manager 1、worker複数、advisor任意
- `fleet.provision`、`task.assign`、`message.send`、`task.report`、`fleet.reconcile`
- Herdr 0.8のlocal workspace/pane/agent操作
- task/event/outboxとruntime bindingのSQLite永続化
- dry-run既定。prompt deliveryが不明なときは自動再送しない

daemon、multi-host、fleet間gateway、独自Web UI、自動pane再作成は対象外である。

## 公開インストール単位

- `agent-fleet-core`: Fleet Spec、logical agent、task、command、event、outbox
- `agent-fleet-herdr`: Herdr 0.8の実行時関連付け、利用者定義のpane配置、指示配送

役割Hookは`agent-fleet-herdr`の内部sidecarであり、利用者は個別にインストールしない。

role catalogは別marketplaceの`agent-roles`を使う。Coreだけを使う場合、Herdr pluginをinstallする必要はない。

## 依存関係

外部pluginは`plugin@marketplace`のidentityで指定する。install commandではversionを固定しない。現在のFleet pluginは0.7.0であり、`roles.harness/v1`のcatalog version 1とHerdr 0.8.xのCLI surfaceを前提とする。

| 依存 | 必須度 | 用途 |
|---|---|---|
| Codex CLIまたはClaude Code | 必須 | marketplaceとpluginのinstall、skill実行 |
| Python 3.10以上 | 必須 | Fleet validator、Core、Herdr Adapter |
| `agent-roles@agent-roles` 0.1.1以上 | Fleet設定の検査・起動で必須 | `role_ref`を検査済みRole Catalogへ解決し、RoleDefinitionの固定版を提供する |
| `agent-fleet-core@agent-fleet` | 必須 | Fleet Specの検査、logical state、task、outboxの管理 |
| `agent-fleet-herdr@agent-fleet` | 任意 | HerdrのRuntimeBinding、ViewPlacement、command配送 |
| Herdr 0.8.x | Herdr Adapterの`--execute`時のみ必須 | local workspace、pane、agentの操作 |
| PyYAMLまたはRuby標準`yaml` | いずれか必須 | Fleet YAMLの安全な読込 |

`agent-fleet-core`は`agent-roles`やHerdrの内部fileを探索・importしない。利用者が`agent-roles`から書き出した検査済みCatalogを`--role-catalog`で明示し、Coreは`role_ref`の存在と必要な権限を解決する。解決したRoleDefinitionは起動時の固定版としてCoreへ保存し、Hookは内容を変更せず会話へ渡す。Herdrとは公開CLI/JSON契約で接続する。

SQLiteはPython標準libraryを使うため、`sqlite3` CLIの追加installは不要である。repositoryの`bash scripts/validate.sh`を実行する開発者は、追加で`bash`、`jq`、Mike Farah `yq` v4、`rg`を用意する。

## インストール

先にRole CatalogとCoreをinstallする。Herdr連携を使う場合だけ、Herdr CLIとAdapterを追加する。

`agent-roles`のSkillで検査済みCatalogを利用者領域へ書き出し、Fleet起動時に次のどちらかで指定する。

```bash
export AGENT_ROLES_CATALOG="$HOME/.config/agent-roles/catalogs/builtin@1.json"
# または fleet-runtimeの各commandへ次を渡す
# --role-catalog "$HOME/.config/agent-roles/catalogs/builtin@1.json"
```

### Codex

```bash
codex plugin marketplace add nakamori-naoya/agent-roles-plugins
codex plugin add agent-roles@agent-roles

codex plugin marketplace add nakamori-naoya/agent-fleet-plugins
codex plugin add agent-fleet-core@agent-fleet

# Herdr連携を使う場合だけ
herdr --version  # 0.8.x
codex plugin add agent-fleet-herdr@agent-fleet
```

CodexではHerdr packageを通常時に無効にし、艦隊を操作するセッションと、そこから起動する艦隊セッションでだけ有効にする。

```toml
[plugins."agent-fleet-herdr@agent-fleet"]
enabled = false
```

艦隊を操作するCodexは`codex --config plugins.agent-fleet-herdr@agent-fleet.enabled=true`で開く。`fleet-runtime start`もHerdrから起動する各Codexへ同じ有効化を渡す。Hookは`AGENT_FLEET_HOOK_RUNTIME`を持つ艦隊agent sessionでだけ役割文脈を注入し、それ以外では何も返さない。

### Claude Code

```bash
claude plugin marketplace add nakamori-naoya/agent-roles-plugins
claude plugin install agent-roles@agent-roles

claude plugin marketplace add nakamori-naoya/agent-fleet-plugins
claude plugin install agent-fleet-core@agent-fleet

# Herdr連携を使う場合だけ
herdr --version  # 0.8.x
claude plugin install agent-fleet-herdr@agent-fleet
```

Claude CodeではHerdr package自体にHookを登録しない。`fleet-runtime start`が各艦隊agentへpackage内のHook sidecarを`--plugin-dir`で渡し、そのsessionだけで読み込む。

Coreだけを利用する場合、Herdr CLIと`agent-fleet-herdr`は不要である。Adapterのdry-runはHerdrを実行しないが、`--execute`を使う前に`herdr --version`が0.8.xであることを確認する。

Fleet YAMLの形式例は[manager 1・worker 2の利用者設定](configs/fleets/release-readiness.yml)にある。最初にCore validatorへFleet YAMLと検査済みRole Catalogを渡す。Coreは各`role_ref`を解決し、RoleDefinitionとCatalogのidentity・内容hashを含むnormalized JSONへ変換する。`fleet-runtime`はこれをHerdr起動設定、表示プロファイル、必要なエージェント起動プロファイルに合成してHerdr Adapterへ渡す。`provision`はdry-runが既定であり、`--execute`を付けない限りHerdrを変更しない。

各メンバーの`runtime`へAI製品、モデル、思考量、代替モデル方針を指定する。開始例はCodexに`gpt-5.6-sol`、Claudeに`claude-fable-5-1`を使い、`fallback: fail`で古いモデルへの暗黙切替を許さない。Herdr Adapterはメンバーごとに`--kind codex`または`--kind claude`を選ぶため、同じ艦隊で両製品を混在できる。Role Catalogは役割の意味だけを持ち、AI製品やモデルを持たない。

艦隊編成、AIアカウントを選ぶエージェント起動プロファイル、Herdr起動設定、pane配置の正本はplugin外の利用者設定である。既定では`~/.config/agent-fleet/fleets`、`~/.config/agent-fleet/agent-command-profiles`、`~/.config/agent-fleet/herdr-launch-profiles`、`~/.config/agent-fleet/view-profiles`を読む。repositoryの[艦隊設定例](configs/fleets/development-squad.yml)、[エージェント起動プロファイル例](configs/agent-command-profiles/codex-personal.v1.yml)、[Herdr起動設定例](configs/herdr-launch-profiles/development-squad-personal.yml)、[表示設定例](configs/view-profiles/role-columns.v1.yml)は手元へ複製して編集するための例であり、pluginは自動読込しない。実行時SQLiteもrepository外のstate directoryへ保存する。

`AgentCommandProfile`は`codex-personal`、`codex-work`、`claude-personal`、`claude-work`のような一つのコマンド名と製品だけを持つ。aliasの展開内容、認証directory、秘密値、モデル引数は複製しない。Herdr起動設定の`spec.agent_command_profiles`が論理メンバーIDから版固定のプロファイルIDを参照し、Herdr Adapterは選ばれたコマンドへ艦隊設定のHook・モデル・思考量を合成する。プロファイルを指定しないメンバーは従来どおり`codex`または`claude`を起動する。

新しいpaneを作る前に、`fleet-runtime`は起動コマンドが利用者の対話シェルで解決できることを確認する。Claude用コマンドは`<command> auth status`、Codex用コマンドは`<command> plugin list --json`も同じアカウント用コマンド経由で検査する。Claudeが未ログインなら複数paneを作らず、`<command> auth login`を一度実行するよう示す。起動済みpaneの制御処理を再開するだけの場合、この起動前検査を繰り返さない。

Herdr起動設定で`spec.codex_hook_trust: preapproved`を明示すると、Herdrから起動する各Codexだけに`--dangerously-bypass-hook-trust`を渡し、役割文脈Hookの起動時レビューを省略する。これはHookの信頼確認だけを省略し、tool承認やsandboxを無効にしない。`review`指定時は対話確認を維持し、プラグイン更新で旧実行ファイルが消えた場合も未確認の新版へ自動移行しない。利用者向けHerdr起動設定例3件は、毎回の艦隊起動を止めないよう`preapproved`を明示している。

Hook登録はHerdr package内のsidecarへ閉じ、個別に配布しない。Hookの実体は艦隊起動時に艦隊stateへ内容address付きで固定配置し、各paneへ`AGENT_FLEET_HOOK_RUNTIME`として渡す。`fleet-runtime`は起動時と再開時に固定版をSHA-256照合する。Hook宣言は固定版を起動するだけに留める。起動中の艦隊は導入元の更新を見に行かず、起動時の固定版で制御処理を再開する。新しいHook実体へ切り替わる境界は停止後の再起動である。

`--execute`は導入元のfleet-controller、Core、Herdr adapter、Hook sourceを一時固定版へ複製し、その一時固定版でFleet・Herdr起動設定・表示プロファイルと実行前提を検査する。fleet-controllerは副作用なしのdry-runで起動し、役割HookのPython構文とClaude用Hook登録の形式も確認する。検査が成功したときだけ、各fileの相対path、mode、size、SHA-256と相対command引数を持つ内容address付きの固定実行版を艦隊stateへ公開する。起動と継続監視はその固定実行版だけを使う。起動中の艦隊は現在のinstall pathや現在版を再捕捉しない。保存済み固定版が欠損または改ざんされている場合は、paneや配送を増やす前に拒否する。

固定実行版の対象は、CoreとHerdr連携部が実行時に必要とするentry point、Python module、schema、defaults、およびClaude用Hook登録の明示allowlistである。`fleet-runtime`自身はこれらの固定実行物を検査・選択する制御境界であり、固定実行版へは含めない。tests、SKILL、README、`__pycache__`、`.pyc`などの実行に不要なfileも含めない。一方、`python3`、Herdr 0.8.x、各AI製品のCLIと認証、Codexの`agent-fleet-herdr`名前解決は外部前提である。Codexの登録宣言は固定した`role_context.py`を起動する薄い境界だけに限り、新しいCodex paneを作る前にHerdr packageが登録済みであることを検査する。

レビューはworkerが`reported`を明示した直後に始め、managerがレビュー結果と報告を照合してから`task.accept`する。したがってreviewer taskをworker taskの`depends_on`に置いて、`accepted`後までレビューを遅らせない。

## 検証

```bash
bash scripts/validate.sh
```
