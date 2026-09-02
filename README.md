# Agent Fleet Plugins

YAMLのFleet Specを正本に、logical agent、task、command、eventを管理し、Herdr 0.8へruntime/view操作を委譲するClaude Code/Codex両対応marketplaceである。

配布単位は`agent-fleet-core`、`agent-fleet-herdr`、`agent-fleet-session-hooks`に分ける。Coreだけを使う利用者へHerdr操作権限を同梱せず、通常セッションへ艦隊専用Hookを登録しない。Fleet Specにはdesired stateだけを記載し、pane IDや実行状態はSQLiteのobserved stateへ分離する。

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
- `agent-fleet-session-hooks`: 艦隊から起動したagent sessionだけで使う役割Hook

role catalogは別marketplaceの`agent-roles`を使う。Coreだけを使う場合、Herdr pluginをinstallする必要はない。

## 依存関係

外部pluginは`plugin@marketplace`のidentityで指定する。install commandではversionを固定しない。現在のFleet pluginは0.2.11であり、`roles.harness/v1`のcatalog version 1とHerdr 0.8.xのCLI surfaceを前提とする。

| 依存 | 必須度 | 用途 |
|---|---|---|
| Codex CLIまたはClaude Code | 必須 | marketplaceとpluginのinstall、skill実行 |
| Python 3.10以上 | 必須 | Fleet validator、Core、Herdr Adapter |
| `agent-roles@agent-roles` | role付きFleet運用では必須 | `manager@1`、`worker@1`などのRoleDefinition |
| `agent-fleet-core@agent-fleet` | 必須 | Fleet Specの検査、logical state、task、outboxの管理 |
| `agent-fleet-herdr@agent-fleet` | 任意 | HerdrのRuntimeBinding、ViewPlacement、command配送 |
| `agent-fleet-session-hooks@agent-fleet` | Codex艦隊では必須 | 艦隊agent sessionだけで役割Hookを登録。通常時はinstall済み・無効にする |
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
codex plugin add agent-fleet-session-hooks@agent-fleet
```

CodexはHook pluginを解決できるようinstallだけ行い、`~/.codex/config.toml`では通常時を無効にする。

```toml
[plugins."agent-fleet-session-hooks@agent-fleet"]
enabled = false
```

`fleet-runtime start`がHerdrから起動する各Codexへ`--config plugins.agent-fleet-session-hooks@agent-fleet.enabled=true`を渡すため、艦隊agent sessionでだけ有効になる。

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

Claude Codeでは`agent-fleet-session-hooks`を通常pluginとしてinstallしない。`fleet-runtime start`が各艦隊agentへ`--plugin-dir`を渡し、そのsessionだけでHook pluginを読み込む。

Coreだけを利用する場合、Herdr CLIと`agent-fleet-herdr`は不要である。Adapterのdry-runはHerdrを実行しないが、`--execute`を使う前に`herdr --version`が0.8.xであることを確認する。

Fleet YAMLの形式例は[manager 1・worker 2の利用者設定](configs/fleets/release-readiness.yml)にある。最初にCore validatorでnormalized JSONへ変換し、Herdr Adapterの`provision`へ渡す。`provision`はdry-runが既定であり、`--execute`を付けない限りHerdrを変更しない。

艦隊編成とpane配置の正本はplugin外の利用者設定である。既定では`~/.config/agent-fleet/fleets`と`~/.config/agent-fleet/view-profiles`を読む。repositoryの[艦隊設定例](configs/fleets/development-squad.yml)と[表示設定例](configs/view-profiles/development-focus.v1.yml)は手元へ複製して編集するための例であり、pluginは自動読込しない。実行時SQLiteもrepository外のstate directoryへ保存する。

Codex艦隊では`spec.runtime.codex_hook_trust: preapproved`を明示すると、Herdrから起動する各Codexだけに`--dangerously-bypass-hook-trust`を渡し、役割文脈Hookの起動時レビューを省略する。これはHookの信頼確認だけを省略し、tool承認やsandboxを無効にしない。省略時と`review`指定時は対話確認を維持し、プラグイン更新で旧実行ファイルが消えた場合も未確認の新版へ自動移行しない。利用者設定例3件は、毎回の艦隊起動を止めないよう`preapproved`を明示している。

Hook登録は`agent-fleet-session-hooks`へ分離し、通常セッションでは読み込まない。Hookの実体は艦隊起動時に艦隊stateへ内容address付きで固定配置し、各paneへ`AGENT_FLEET_HOOK_RUNTIME`として渡す。`fleet-runtime`は起動時と再開時に固定版をSHA-256照合する。Hook plugin内の`hooks/*.json`は検査済みの固定版を起動する一行だけを持つ。既存sessionは起動時の実体を使い続けるため、plugin cacheの旧versionが削除されても実行pathを失わない。Hook実体を更新する場合は、艦隊を停止して再起動する。

## 検証

```bash
bash scripts/validate.sh
```
