---
name: fleet-herdr-runtime
description: logical agent_refをHerdr 0.8のworkspace、tab、pane、agentへ安全にbindし、Agent Fleet commandを配信する。
---

# Fleet Herdr adapter

## 利用者向けの統合入口

複数のFleet設定を保持して選択起動する場合は`scripts/fleet-runtime`を使う。既定では`~/.config/agent-fleet/fleets`と`~/.config/agent-fleet/view-profiles`を読む。別の場所は`--fleet-dir`と`--profile-dir`で指定する。

`init`は利用者の設定・状態directoryだけを作り、Fleetを埋め込まない。`doctor`は設定directory、Core CLI、Herdr、状態directoryを診断する。`list`は設定と解決状態を列挙する。`plan <fleet_id>`はファイル、内容要約値、pane計画を返し、DBもdirectoryも作らない。`start <fleet_id> --execute`はCore、Herdr、役割文脈、初期タスクを冪等に準備し、paneを持たない配送制御をforegroundで続ける。`status <fleet_id>`はCoreとHerdrの公開CLIを通して状態を結合する。同じ内容で再起動した場合はpaneを増やさず配送制御を再開し、内容が変わっていれば暗黙適用せず競合として止める。`stop`はCoreの履歴を残してworkspaceを閉じ、`remove`は再構成のためCore状態も明示的に削除する。

`RuntimeBinding` と `ViewPlacement` は `--state-db` で指定したadapter専用SQLiteへ保存する。Core DBにはpane IDを入れない。paneが見つからない場合はbindingを `lost` にして停止し、`bind` または `rebind` で明示的に修復する。MVPのreconcileはpane lostを検出するだけで自動再配置しない。

`provision --fleet-json '<normalized Fleet JSON>' --view-profile-json '<validated ViewProfile JSON>' --cwd <path> --agent-kind <kind>` は、Fleetの版固定`profile_ref`とProfile identityの一致、人数制約、layout treeを検査する。Profileのweightを決定的なHerdr 0.8逐次splitへ変換し、execute中にworkspace/tab/pane IDを解析できなければbindingやviewを保存せず停止する。同じFleet、Profile、memberのbindingが揃っていれば`already_provisioned`を返し、別Profileとの暗黙上書きは拒否する。

`status --fleet <fleet_id>` はbinding、placement、`profile_ref`を公開JSONで返す読み取り専用操作である。HerdrへのprobeやSQLite更新は行わない。provisionのdry-runも指定されたstate DBや親directoryを作成しない。

`scripts/fleet-herdr ... dispatch --request-json '<Core outbox JSON>'` は公開CLI/JSON境界でCore requestを受ける。Core pluginのfileをimport・相対参照しない。dry-runが既定で、生成したargvだけをJSON表示する。`--execute` がある場合だけ `shell=False` 相当のargvでHerdr 0.8 CLIを実行する。HookのCore照合が成功すれば、その受領を配送済みとして確定する。受領を確認できないprompt timeoutは`unknown`として自動retryしない。task完了はagentの明示的な `task.report` をCoreへ反映して確定する。

`context.sync`は通常promptとは別扱いである。Hookはprompt中のargvや役割文脈を信頼せず、Coreが発行した一回限りtokenを、環境から解決した信頼済みCore CLIで消費して正本を取得する。通常指示もCoreに保存された指示ID、内容、送信元、宛先、受信sessionを照合し、成功した場合だけ現在の役割文脈とともに処理する。

daemon、multi-host、fleet gateway、独自TUIは対象外である。
