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
- `agent-fleet-herdr`: Herdr 0.8のRuntimeBinding、ViewPlacement、command-deck、command配送

role catalogは別marketplaceの`agent-roles`を使う。Coreだけを使う場合、Herdr pluginをinstallする必要はない。

```bash
codex plugin marketplace add nakamori-naoya/agent-fleet-plugins
codex plugin add agent-fleet-core@agent-fleet
codex plugin add agent-fleet-herdr@agent-fleet
```

Fleet YAMLの例は[manager 1・worker 2のFleet Spec](plugins/agent-fleet-core/spec/examples/fleet.example.yml)にある。最初にCore validatorでnormalized JSONへ変換し、Herdr Adapterの`provision`へ渡す。`provision`はdry-runが既定であり、`--execute`を付けない限りHerdrを変更しない。

設定の正本は[Core defaults](plugins/agent-fleet-core/config/defaults.yml)と[Herdr defaults](plugins/agent-fleet-herdr/config/defaults.yml)である。実行時SQLiteはrepository外のstate directoryを指定する。

## 検証

```bash
bash scripts/validate.sh
```
