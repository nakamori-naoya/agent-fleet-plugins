# Agent Fleetのコンセプトと設計

この資料はAgent Fleetの責務分離、安全境界、状態管理を説明する。起動手順は[README](../README.md)を使う。

## Fleet YAMLが望む編成を表す

Fleet YAMLは、誰をどの役割・製品・command・モデルで動かし、何を完了とするかを表す。pane ID、workspace ID、実行中のtask状態は書き戻さない。

各memberの起動条件は`runtime`へまとめる。設定ファイルはどのdirectoryにも置ける。ViewProfileだけは表示方法として交換でき、省略時はplugin既定値を使う。

## Role Catalogは役割の意味だけを共有する

`agent-roles`が管理するRole Catalogは、manager、worker、advisor、reviewerの責任・権限・禁止事項を定義する。Fleetは版付き`role_ref`で参照し、Role本文を複製しない。

AI製品、アカウントを選ぶcommand、モデル、思考量はRoleではなくFleetが所有する。同じadvisor roleをCodexにもClaudeにも割り当てられる。

## CoreとHerdr Adapterは異なる状態を持つ

`agent-fleet-core`は論理agent、task、command、event、報告、受入状態をSQLiteで管理する。Herdrのworkspaceやpane配置を知らない。

`agent-fleet-herdr`は論理agentとHerdr paneのbinding、ViewProfileから作った配置、指示配送を管理する。paneの待機表示や終了表示からtask完了を推測しない。task完了はagentの明示報告、最終完了はmanagerの受入で確定する。

この分離により、Coreだけを使う環境へHerdr操作権限や役割Hookを持ち込まずに済む。

## 起動前と起動直後に境界を検査する

`start --execute`はworkspaceを作る前に、Core、Herdr、Hook、起動command、モデル設定、Claude認証、Codex plugin登録を検査する。検査済みの実行物は内容hash付きでstateへ固定し、起動中にinstall元が変わっても自動で切り替えない。

Fleet YAMLの`metadata.id`は再利用可能なdefinition IDである。起動ごとに別のrun IDを発行し、`state/runs/<run-id>/`へmanifest、Core DB、Herdr DB、設定snapshot、実行物snapshotを閉じ込める。`state/registry.sqlite3`はrunを検索するための再構築可能な索引であり、実行状態の正本ではない。この境界により同じ定義を同時に複数起動しても、task、command、hook context、workspaceは交差しない。

workspace作成後はHerdrからlayoutを読み直し、次を検査する。

- Fleet member数とpane数
- agentとpane IDの一対一対応
- 各paneの位置、幅、高さ
- 表示領域からのはみ出し、pane同士の重なり、隙間
- ViewProfileから計算したsplit数、方向、比率の結果となる期待矩形

不一致ならbindingとplacementを保存せず、作成したworkspaceを閉じる。

## 配送結果が不明な指示は自動再送しない

指示はCoreのOutboxから論理`agent_ref`へ送られ、Herdr Adapterが現在のpaneを解決する。HookがCore上の内容・送信元・宛先・受信sessionを照合した時点で配送済みになる。

prompt送信が時間切れになり、受領済みか判断できない場合は`unknown`として止める。自動再送すると同じ指示を二度実行する可能性があるためである。

## 現在の対象範囲

対象はlocal Herdr 0.8上のmanager、worker、advisor、reviewerによる艦隊である。task、報告、レビュー、manager受入、pane binding、指示配送を扱う。

daemon、複数host、艦隊間gateway、独自Web UI、消えたpaneの自動再作成は対象外である。

## 詳細設計

- [艦隊の連携と全体制御](2026-08-31-エージェント艦隊-連携と全体制御.md)
- [作業進行ルール](2026-08-31-エージェント艦隊-作業進行ルール.md)
- [非機能要件](2026-08-31-エージェント艦隊-非機能要件.md)
- [役割Hookの実行契機と責務](2026-09-01-エージェント艦隊-役割Hookの実行契機と責務.md)
