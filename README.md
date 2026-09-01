# Agent Fleet Plugins

YAMLのFleet Specを正本に、logical agent、task、command、eventを管理し、Herdr 0.8へruntime/view操作を委譲するClaude Code/Codex両対応marketplaceである。

配布単位は`agent-fleet-core`と`agent-fleet-herdr`に分ける。Coreだけを使う利用者へHerdr操作権限を同梱せず、両者は公開CLI/JSON契約で接続する。Fleet Specにはdesired stateだけを記載し、pane IDや実行状態はSQLiteのobserved stateへ分離する。

## MVP境界

- manager 1、worker複数、advisor任意
- `fleet.provision`、`task.assign`、`message.send`、`task.report`、`fleet.reconcile`
- Herdr 0.8のlocal workspace/pane/agent操作
- task/event/outboxとruntime bindingのSQLite永続化
- dry-run既定。prompt deliveryが不明なときは自動再送しない

daemon、multi-host、fleet間gateway、独自Web UI、自動pane再作成は対象外である。

## Installable plugin

- `agent-fleet-core`: Fleet Spec、logical agent、task、command、event、outbox
- `agent-fleet-herdr`: Herdr 0.8の実行時関連付け、利用者定義のpane配置、指示配送

role catalogは別marketplaceの`agent-roles`を使う。Coreだけを使う場合、Herdr pluginをinstallする必要はない。

## 依存関係

外部pluginは`plugin@marketplace`のidentityで指定する。install commandではversionを固定しない。現在のFleet pluginは0.2.6であり、`roles.harness/v1`のcatalog version 1とHerdr 0.8.xのCLI surfaceを前提とする。

| 依存 | 必須度 | 用途 |
|---|---|---|
| Codex CLIまたはClaude Code | 必須 | marketplaceとpluginのinstall、skill実行 |
| Python 3.10以上 | 必須 | Fleet validator、Core、Herdr Adapter |
| `agent-roles@agent-roles` | role付きFleet運用では必須 | `manager@1`、`worker@1`などのRoleDefinition |
| `agent-fleet-core@agent-fleet` | 必須 | Fleet Specの検査、logical state、task、outboxの管理 |
| `agent-fleet-herdr@agent-fleet` | 任意 | HerdrのRuntimeBinding、ViewPlacement、command配送 |
| Herdr 0.8.x | Herdr Adapterの`--execute`時のみ必須 | local workspace、pane、agentの操作 |
| PyYAMLまたはRuby標準`yaml` | いずれか必須 | Fleet YAMLの安全な読込 |

`agent-fleet-core`は`agent-roles`やHerdrの内部fileをimportしない。`role_ref`の形式だけを検査し、Herdrとは公開CLI/JSON契約で接続する。このため、利用者が必要な依存pluginを明示的にinstallする。

SQLiteはPython標準libraryを使うため、`sqlite3` CLIの追加installは不要である。repositoryの`bash scripts/validate.sh`を実行する開発者は、追加で`bash`、`jq`、Mike Farah `yq` v4、`rg`を用意する。

## インストール

先にRole CatalogとCoreをinstallする。Herdr連携を使う場合だけ、Herdr CLIとAdapterを追加する。

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

Coreだけを利用する場合、Herdr CLIと`agent-fleet-herdr`は不要である。Adapterのdry-runはHerdrを実行しないが、`--execute`を使う前に`herdr --version`が0.8.xであることを確認する。

Fleet YAMLの形式例は[manager 1・worker 2の利用者設定](configs/fleets/release-readiness.yml)にある。最初にCore validatorでnormalized JSONへ変換し、Herdr Adapterの`provision`へ渡す。`provision`はdry-runが既定であり、`--execute`を付けない限りHerdrを変更しない。

艦隊編成とpane配置の正本はplugin外の利用者設定である。既定では`~/.config/agent-fleet/fleets`と`~/.config/agent-fleet/view-profiles`を読む。repositoryの[艦隊設定例](configs/fleets/development-squad.yml)と[表示設定例](configs/view-profiles/development-focus.v1.yml)は手元へ複製して編集するための例であり、pluginは自動読込しない。実行時SQLiteもrepository外のstate directoryへ保存する。

## 検証

```bash
bash scripts/validate.sh
```
