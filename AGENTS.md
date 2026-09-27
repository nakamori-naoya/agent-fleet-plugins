> 共通の規約は /Users/naoya-nakamoriq/Documents/Github/harness-pluginsv2/AGENTS.md にある。ここには、この repository だけの規則を置く。

# AGENTS.md

このrepositoryは、YAMLで定義したagent fleetを論理agent単位で制御する`agent-fleet` marketplaceのsourceである。Fleetをどう検査し、起動し、完了を判断するかは、二つの入口の SKILL.md が持つ。ここには repository の構成の規則だけを置く。

- installable pluginは`agent-fleet-core`と`agent-fleet-herdr`に分け、CoreへHerdr操作権限を同梱しない。CoreはHerdr pluginのfileを相対参照せず、二つは版付きの公開JSON契約だけでつなぐ。
- `internal/agent-fleet-session-hooks`はHerdrが所有するhook専用の内部sidecarであり、`internalPlugins`で宣言しmarketplace entryへ公開しない。Hook実装を別pluginや別domainへ複製しない。
- 自動testで実Herdrを変更しない。
