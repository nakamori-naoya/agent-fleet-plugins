# AGENTS.md

このrepositoryは、YAMLで定義したagent fleetを論理agent単位で制御する`agent-fleet` marketplaceのsourceである。

- installable pluginは`agent-fleet-core`と`agent-fleet-herdr`に分け、CoreへHerdr操作権限を同梱しない。
- Fleet Specはdesired stateだけを持ち、pane ID、workspace ID、task実行状態を書き戻さない。
- Fleet CoreはHerdr commandやUI geometryを知らず、logical agent、task、command、eventを扱う。Herdr pluginのfileを相対参照しない。
- Herdr Adapterは公開CLI/JSON契約だけでCoreと接続し、bindingとobserved viewを扱う。dry-runを既定にし、自動testで実Herdrを変更しない。
- task完了は明示reportを正本とし、pane出力やidle状態から推測しない。
- daemon、multi-host、fleet間連携、独自Web UIはMVPへ含めない。
- install cacheは編集せず、このsourceを正本として変更する。
- 変更後は`bash scripts/validate.sh`を実行する。
