---
name: fleet-herdr-runtime
description: logical agent_refをHerdr 0.8のworkspace、tab、pane、agentへ安全にbindし、Agent Fleet commandを配信する。
---

# Fleet Herdr adapter

## 利用者向けの統合入口

Fleetを起動する場合は`scripts/fleet-runtime plan /absolute/path/to/fleet.yml`と`scripts/fleet-runtime start /absolute/path/to/fleet.yml --execute`を使う。Fleetファイルは任意のdirectoryに置ける。各memberの起動コマンドとモデルはFleetに置き、`spec.view_profile`を省略するとplugin既定ViewProfileを使う。役割定義は`agent-roles`が書き出した検査済みCatalogを`--role-catalog`または`AGENT_ROLES_CATALOG`で明示し、既定の`~/.config/agent-roles/catalogs/builtin@1.json`が存在すればそれを使う。

`plan <absolute-fleet-path>`は艦隊、表示、起動コマンド、内容要約値、pane計画を返し、DBもdirectoryも作らない。`start <absolute-fleet-path> --execute`は新しいrunとしてCore、Herdr、役割文脈、初期タスクを準備し、paneを持たない配送制御をforegroundで続ける。`status <run-id>`はCoreとHerdrの公開CLIを通して対象runの状態を結合する。

`start --execute`は起動ごとに一意なrun IDを発行する。同じFleet YAMLを繰り返し起動してよく、各runのworkspace、Core DB、Herdr DB、task、command、hook contextを分離する。YAMLの`metadata.id`はdefinition IDであり、実行操作には`runs [definition-id]`で得たrun IDを使う。`status`、`stop --execute`、`remove --execute`はrun IDを対象とし、controllerだけが終了したactive runは`resume <run-id>`で再開する。

`RuntimeBinding` と `ViewPlacement` は `--state-db` で指定したadapter専用SQLiteへ保存する。Core DBにはpane IDを入れない。paneが見つからない場合はbindingを `lost` にして停止し、`bind` または `rebind` で明示的に修復する。MVPのreconcileはpane lostを検出するだけで自動再配置しない。

`provision --fleet-json '<Fleet JSON>' --view-profile-json '<ViewProfile JSON>' --cwd <path>` は、起動設定、人数制約、全メンバーの一意な列割当を検査する。起動コマンドはHerdr paneの対話シェルで実行するため、alias・shell function・PATH上の実行可能ファイルを選べる。Profileのweightを決定的なHerdr 0.8逐次splitへ変換し、workspace作成後にpane数、pane ID、各矩形の幅・高さ・位置・被覆、split方向・比率を再取得して検査する。一つでも一致しなければbindingやviewを保存せずworkspaceを閉じる。同じFleet、Profile、member、設定文書の内容、作業directory、起動条件の合成hashが揃っていれば`already_provisioned`を返し、同一版名の内容変更を含む暗黙上書きは拒否する。

`status --fleet <fleet_id>` はbinding、placement、`profile_ref`を公開JSONで返す読み取り専用操作である。HerdrへのprobeやSQLite更新は行わない。provisionのdry-runも指定されたstate DBや親directoryを作成しない。

`scripts/fleet-herdr ... dispatch --request-json '<Core outbox JSON>'` は公開CLI/JSON境界でCore requestを受ける。Core pluginのfileをimport・相対参照しない。dry-runが既定で、生成したargvだけをJSON表示する。`--execute` がある場合だけ `shell=False` 相当のargvでHerdr 0.8 CLIを実行する。HookのCore照合が成功すれば、その受領を配送済みとして確定する。受領を確認できないprompt timeoutは`unknown`として自動retryしない。task完了はagentの明示的な `task.report` をCoreへ反映して確定する。

`context.sync`は通常promptとは別扱いである。Hookはprompt中のargvや役割文脈を信頼せず、Coreが発行した一回限りtokenを、環境から解決した信頼済みCore CLIで消費して正本を取得する。通常指示もCoreに保存された指示ID、内容、送信元、宛先、受信sessionを照合し、成功した場合だけ現在の役割文脈とともに処理する。

daemon、multi-host、fleet gateway、独自TUIは対象外である。
