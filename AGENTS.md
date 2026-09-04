# AGENTS.md

このrepositoryは、YAMLで定義したagent fleetを論理agent単位で制御する`agent-fleet` marketplaceのsourceである。

- installable pluginは`agent-fleet-core`と`agent-fleet-herdr`に分け、CoreへHerdr操作権限を同梱しない。
- Fleet Specはdesired stateだけを持ち、pane ID、workspace ID、task実行状態を書き戻さない。
- AIアカウントを切り替えるコマンドはHerdrやFleetへ直書きせず、Herdr非依存のAgentCommandProfileへ一つのコマンド名として置く。Herdr LaunchProfileは必要なメンバーIDから版固定profileを参照する。
- Fleet CoreはHerdr commandやUI geometryを知らず、logical agent、task、command、eventを扱う。Herdr pluginのfileを相対参照しない。
- Herdr Adapterは公開CLI/JSON契約だけでCoreと接続し、bindingとobserved viewを扱う。dry-runを既定にし、自動testで実Herdrを変更しない。
- `--execute`の起動は、fleet-controller、Core、Herdr adapter、Hook sourceをstateやHerdr workspaceの作成前に検査する。runtime manifestには各実行物の内容hashを保存し、同じfleet IDでidentityが異なる再開は拒否する。
- task完了は明示reportを正本とし、pane出力やidle状態から推測しない。
- session-hooks-pluginはHerdrが所有する同梱sidecarであり、marketplace entryはCodexで有効化する配布面にすぎない。Hook実装を別pluginや別domainへ複製しない。
- reviewerはworkerの`accepted`を`depends_on`にせず、workerが`reported`になった時点でレビューする。managerはレビュー後にのみ`task.accept`する。
- daemon、multi-host、fleet間連携、独自Web UIはMVPへ含めない。
- install cacheは編集せず、このsourceを正本として変更する。
- 変更後は`bash scripts/validate.sh`を実行する。
