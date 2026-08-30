---
name: provision-herdr-fleet
description: 検査済みFleet JSONからHerdr 0.8のworkspace、tab、pane、agent編成を計画・適用し、logical agentへcommandを配送する。Fleet Specやroleは定義しない。
---

# provision-herdr-fleet

HerdrはRuntime AdapterとView Adapterであり、Fleetの正本ではない。`agent-fleet-core`が出した検査済みFleet JSONまたは`fleet.harness/v1` Command envelopeだけを入力にする。Core pluginのfileを探索・相対参照しない。

## 先に計画を確認する

詳しいCLIと復旧規則は`adapter/SKILL.md`を全文読む。入口は`adapter/scripts/fleet-herdr`で、dry-runが既定である。

`provision`はcommand-deck profileを、manager左約32%、member右側の再現可能なsplit計画へ変換する。生成argvとlogical agentの配置を確認してから、利用者が明示した場合だけ`--execute`を使う。

## Bindingと配送

RuntimeBindingとViewPlacementはadapter専用SQLiteへ保存する。Core DBやFleet Specへpane IDを書き戻さない。managerはpane IDではなくlogical `agent_ref`へCommand envelopeを送り、adapterが現在のbindingを解決する。

paneが消えたら`lost`として報告し、自動再作成・自動再bindを行わない。prompt timeoutは`unknown`として返し、自動retryしない。task完了はCoreへの明示`task.report`だけで確定する。

実行対象はlocal Herdr 0.8に限定する。daemon、multi-host、fleet間gateway、独自Web UIは対象外である。
