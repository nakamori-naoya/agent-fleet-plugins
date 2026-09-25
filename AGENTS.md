> 共通の規約は /Users/naoya-nakamoriq/Documents/Github/harness-pluginsv2/AGENTS.md にある。ここには、この repository だけの規則を置く。

# AGENTS.md

このrepositoryは、YAMLで定義したagent fleetを論理agent単位で制御する`agent-fleet` marketplaceのsourceである。

- installable pluginは`agent-fleet-core`と`agent-fleet-herdr`に分け、CoreへHerdr操作権限を同梱しない。
- Fleet Specはdesired stateだけを持ち、pane ID、workspace ID、task実行状態を書き戻さない。
- Fleet YAMLは各memberの`runtime`にAI製品、起動コマンド、モデル、思考量、fallbackをまとめ、絶対パスで起動する。Fleet YAMLのファイルパス型fieldへ値を書く場合も絶対パスだけを使い、相対パス、`~`、環境変数による省略を使わない。
- Fleet CoreはHerdr commandやUI geometryを知らず、logical agent、task、command、eventを扱う。Herdr pluginのfileを相対参照しない。
- Herdr Adapterは公開CLI/JSON契約だけでCoreと接続し、bindingとobserved viewを扱う。dry-runを既定にし、自動testで実Herdrを変更しない。
- `--execute`の起動は、fleet-controller、Core、Herdr adapter、Hook sourceをstateやHerdr workspaceの作成前に検査する。起動ごとに一意なrun IDを発行し、runtime manifestにはdefinition IDと各実行物の内容hashを保存する。同じFleet定義の複数runは相互に隔離する。
- Herdr workspace作成後は、Fleet人数とpane数、一対一のpane ID、矩形の幅・高さ・位置・重なり・空白、split数・方向・比率を検査し、一致しない場合はbindingを保存せずworkspaceを閉じる。
- task完了は明示reportだけを判断根拠とし、pane出力やidle状態から推測しない。
- `internal/agent-fleet-session-hooks`はHerdrが所有するhook専用の内部sidecarであり、`internalPlugins`で宣言しmarketplace entryへ公開しない。CodexではHerdr plugin、ClaudeではHerdrが渡す内部pathを艦隊sessionだけで有効にする。Hook実装を別pluginや別domainへ複製しない。
- reviewerはworkerの`accepted`を`depends_on`にせず、workerが`reported`になった時点でレビューする。managerはレビュー後にのみ`task.accept`する。
- daemon、multi-host、fleet間連携、独自Web UIはMVPへ含めない。
