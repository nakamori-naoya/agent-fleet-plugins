---
name: fleet-herdr-runtime
description: logical agent_refをHerdr 0.8のworkspace、tab、pane、agentへ安全にbindし、Agent Fleet commandを配信する。
---

# Fleet Herdr adapter

## 利用者向けの統合入口

複数の艦隊を選択起動する場合は`scripts/fleet-runtime`を使う。既定では`~/.config/agent-fleet/fleets`、`~/.config/agent-fleet/herdr-launch-profiles`、`~/.config/agent-fleet/view-profiles`を読む。別の場所は`--fleet-dir`、`--launch-dir`、`--profile-dir`で指定する。役割定義は`agent-roles`が書き出した検査済みCatalogを`--role-catalog`または`AGENT_ROLES_CATALOG`で明示する。pluginのinstall先を暗黙探索しない。

`init`は利用者の設定・状態directoryだけを作り、設定実体を埋め込まない。`doctor`は設定directory、Core CLI、Herdr、状態directoryを診断する。`list`は起動設定と解決状態を列挙する。`plan <launch_id>`は艦隊、Herdr起動、表示、必要なエージェント起動プロファイル、内容要約値、pane計画を返し、DBもdirectoryも作らない。`start <launch_id> --execute`はCore、Herdr、役割文脈、初期タスクを冪等に準備し、paneを持たない配送制御をforegroundで続ける。`status <launch_id>`はCoreとHerdrの公開CLIを通して状態を結合する。同じ内容で再起動した場合はpaneを増やさず配送制御を再開し、内容が変わっていれば暗黙適用せず競合として止める。旧Fleet v1を変換する場合だけ`--legacy-fleet`を明示する。

`RuntimeBinding` と `ViewPlacement` は `--state-db` で指定したadapter専用SQLiteへ保存する。Core DBにはpane IDを入れない。paneが見つからない場合はbindingを `lost` にして停止し、`bind` または `rebind` で明示的に修復する。MVPのreconcileはpane lostを検出するだけで自動再配置しない。

`provision --fleet-json '<Portable Fleet JSON>' --launch-profile-json '<Herdr LaunchProfile JSON>' --agent-command-profiles-json '<メンバー別の解決済み起動コマンドJSON>' --view-profile-json '<ViewProfile JSON>' --cwd <path>` は、LaunchProfileの`fleet_ref`、`view_profile_ref`、`agent_command_profiles`が入力実体に一致すること、人数制約、全メンバーの一意な列割当を検査する。起動プロファイルを指定したメンバーはHerdr paneの対話シェルでそのコマンドを実行するため、alias・shell function・PATH上の実行可能ファイルを選べる。各メンバーの`runtime.product`、`runtime.model`、`runtime.effort`は製品別の起動引数として後から合成する。`fallback: fail`のClaudeにはモデル自動切替を無効にする設定を渡す。Profileのweightを決定的なHerdr 0.8逐次splitへ変換し、execute中にworkspace/tab/pane IDを解析できなければbindingやviewを保存せず停止する。同じFleet、Profile、member、設定文書の内容、作業directory、起動条件の合成hashが揃っていれば`already_provisioned`を返し、同一版名の内容変更を含む暗黙上書きは拒否する。

`status --fleet <fleet_id>` はbinding、placement、`profile_ref`を公開JSONで返す読み取り専用操作である。HerdrへのprobeやSQLite更新は行わない。provisionのdry-runも指定されたstate DBや親directoryを作成しない。

`scripts/fleet-herdr ... dispatch --request-json '<Core outbox JSON>'` は公開CLI/JSON境界でCore requestを受ける。Core pluginのfileをimport・相対参照しない。dry-runが既定で、生成したargvだけをJSON表示する。`--execute` がある場合だけ `shell=False` 相当のargvでHerdr 0.8 CLIを実行する。HookのCore照合が成功すれば、その受領を配送済みとして確定する。受領を確認できないprompt timeoutは`unknown`として自動retryしない。task完了はagentの明示的な `task.report` をCoreへ反映して確定する。

`context.sync`は通常promptとは別扱いである。Hookはprompt中のargvや役割文脈を信頼せず、Coreが発行した一回限りtokenを、環境から解決した信頼済みCore CLIで消費して正本を取得する。通常指示もCoreに保存された指示ID、内容、送信元、宛先、受信sessionを照合し、成功した場合だけ現在の役割文脈とともに処理する。

daemon、multi-host、fleet gateway、独自TUIは対象外である。
