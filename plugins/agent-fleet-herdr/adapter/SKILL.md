---
name: fleet-herdr-runtime
description: logical agent_refをHerdr 0.8のworkspace、tab、pane、agentへ安全にbindし、Agent Fleet commandを配信する。
---

# Fleet Herdr adapter

`RuntimeBinding` と `ViewPlacement` は `--state-db` で指定したadapter専用SQLiteへ保存する。Core DBにはpane IDを入れない。paneが見つからない場合はbindingを `lost` にして停止し、`bind` または `rebind` で明示的に修復する。MVPのreconcileはpane lostを検出するだけで自動再配置しない。

`provision --fleet-json '<normalized Fleet JSON>' --cwd <path> --agent-kind <kind>` は `fleet.harness/v1` Fleet、Herdr provider、`command-deck` view profile、1 managerと1〜4 workersを検査する。managerを左32%、workersを同じtabの右側へ配置する決定的なHerdr 0.8 argv planを返す。execute中にHerdr出力からworkspace/tab/pane IDを解析できなければ、bindingやviewを保存せず停止する。

`scripts/fleet-herdr ... dispatch --request-json '<Core outbox JSON>'` は公開CLI/JSON境界でCore requestを受ける。Core pluginのfileをimport・相対参照しない。dry-runが既定で、生成したargvだけをJSON表示する。`--execute` がある場合だけ `shell=False` 相当のargvでHerdr 0.8 CLIを実行する。prompt timeoutは配信・完了状態が `unknown` なので自動retryしない。task完了はagentの明示的な `task.report` をCoreへ反映して確定する。

daemon、multi-host、fleet gateway、独自TUIは対象外である。
