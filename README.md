# Agent Fleet Plugins

YAMLでmanager、worker、advisor、reviewerと使用モデルを定義し、Herdr上でClaude CodeとCodexの艦隊を起動するpluginである。

このREADMEは、Fleetを設定して起動・確認・停止する手順を扱う。責務分離や状態管理の考え方は[コンセプトと設計](docs/concepts.md)に分けている。

## 必要なもの

- Python 3.10以上
- Herdr 0.8.x
- Claude CodeまたはCodex CLI
- `agent-roles@agent-roles`
- `agent-fleet-core@agent-fleet`
- `agent-fleet-herdr@agent-fleet`
- `~/.config/agent-roles/catalogs/builtin@1.json`に書き出したRole Catalog

このsource checkoutから操作する場合は、リポジトリ直下の`scripts/fleet-runtime`を使う。以下の例もリポジトリ直下で実行する。

## pluginをインストールする

Codexでは次を実行する。

```bash
codex plugin marketplace add nakamori-naoya/agent-roles-plugins
codex plugin add agent-roles@agent-roles
codex plugin marketplace add nakamori-naoya/agent-fleet-plugins
codex plugin add agent-fleet-core@agent-fleet
codex plugin add agent-fleet-herdr@agent-fleet
```

Claude Codeでは次を実行する。

```bash
claude plugin marketplace add nakamori-naoya/agent-roles-plugins
claude plugin install agent-roles@agent-roles
claude plugin marketplace add nakamori-naoya/agent-fleet-plugins
claude plugin install agent-fleet-core@agent-fleet
claude plugin install agent-fleet-herdr@agent-fleet
```

Role Catalogを既定以外へ置く場合は、絶対パスを環境変数へ指定する。

```bash
export AGENT_ROLES_CATALOG="/absolute/path/to/builtin@1.json"
```

## 使用するCLIを準備する

Fleetは各agentの`runtime.command`に指定したコマンドを、そのまま利用者の対話shellで解決して起動する。通常は`claude`または`codex`を指定できる。複数アカウントを切り替えるwrapperやaliasを使う場合は、そのコマンド名を指定する。

起動前に、使用する各CLIを単独で実行でき、必要な認証とplugin設定が完了していることを確認する。認証方法はCLIの提供元や利用形態によって異なるため、それぞれの公式手順に従う。

Fleetはpaneを作る前に、設定された起動コマンド、認証状態、必要なpluginを検査する。準備できていないコマンドが一つでもあれば、workspaceを作らず終了する。

## Fleet YAMLを1ファイル用意する

Fleet YAMLは`fleet.harness/v3`を使う。置き場所は自由だが、`plan`と`start`には絶対パスを渡す。

Fleet YAMLのファイルパス型fieldへ値を書く場合も絶対パスを使う。相対パス、`~`、環境変数による省略は使わない。

各memberの`runtime`には次の5項目を指定する。

| 項目 | 例 | 意味 |
|---|---|---|
| `product` | `codex` | 起動する製品 |
| `command` | `codex` | 実行可能なCLIコマンド、wrapper、またはalias |
| `model` | `<model-id>` | 利用者が契約・利用できるモデルID |
| `effort` | `high` | 思考量 |
| `fallback` | `fail` | 指定モデルを使えない場合の扱い |

最小構成は次の形である。既定ViewProfileは3〜7人に対応する。

```yaml
apiVersion: fleet.harness/v3
kind: Fleet
metadata:
  id: mixed-review
spec:
  codex_hook_trust: preapproved
  objective: 変更を実装し、独立レビューを完了する。
  completion_criteria:
    - managerが検証結果を確認して成果を受け入れている。
  stop_conditions:
    - 追加承認が必要な破壊的変更がある。
  members:
    - agent_ref: manager
      role_ref: manager@1
      runtime: {product: claude, command: claude, model: "your-claude-model-id", effort: high, fallback: fail}
    - agent_ref: worker
      role_ref: worker@1
      runtime: {product: codex, command: codex, model: "your-codex-model-id", effort: medium, fallback: fail}
    - agent_ref: advisor
      role_ref: advisor@1
      runtime: {product: codex, command: codex, model: "your-codex-model-id", effort: high, fallback: fail}
  tasks:
    - id: implementation
      assignee: worker
      depends_on: []
      instructions: 要求を実装して検証する。
      expected_output: 実装差分と検証結果。
      completion_criteria:
        - 自動テストが成功している。
  collaboration:
    manager: manager
    advisor: advisor
    reporting: {strategy: manager, include_task_updates: true}
```

Roleの内容はFleetへ複製しない。`role_ref`は共通Role Catalogの`manager@1`、`worker@1`、`advisor@1`、`reviewer@1`を参照する。

`your-claude-model-id`と`your-codex-model-id`は例示用のプレースホルダーである。利用するCLIで有効なモデルIDへ置き換える。

## planしてから起動する

まずFleet YAMLの絶対パスを変数へ入れる。

```bash
FLEET="/absolute/path/to/fleet.yml"
```

設定、モデル、起動コマンド、pane構成をdry-runで確認する。この操作はworkspaceやstateを作らない。

```bash
scripts/fleet-runtime plan "$FLEET"
```

問題がなければ起動する。起動後は配送制御がforegroundで動き続ける。

```bash
scripts/fleet-runtime start "$FLEET" --execute
```

起動時には、Fleet人数とpane数、agentとpaneの一対一対応、paneの位置・幅・高さ、split方向と比率の結果を実際のHerdr表示から検査する。不一致ならbindingを保存せず、作成したworkspaceを閉じる。

## 起動ごとのrunを確認・停止する

`start --execute`は毎回、新しいrun IDを発行する。同じFleet YAMLから複数の艦隊を同時に起動でき、それぞれ独立したHerdr workspace、Core DB、Herdr DB、task状態を持つ。起動結果の`run_id`を以後の操作に使う。

```bash
scripts/fleet-runtime runs mixed-review
scripts/fleet-runtime status mixed-review-0123456789abcdef0123456789abcdef
scripts/fleet-runtime stop mixed-review-0123456789abcdef0123456789abcdef --execute
```

## task報告statusの構造検査宣言

- 正本: Coreの`TASK_TRANSITIONS`と公開`control-agent-fleet` Skill
- 入力: `fleet-control task.report --status`と、割当済みtaskを持つ一時SQLite DB
- 正規化: statusはaliasへ変換せず、CLIへ渡された文字列をそのまま遷移集合と照合する
- 合格述語: workerの成果報告は`reported`で受理され、managerの`task.accept`後だけ`accepted`になる。`completed`は公開statusではなく操作前に拒否される
- 診断: 未知statusはargparseが不正な選択肢としてexit 2を返し、task状態を変えない
- 正例: `running -> reported -> accepted`。反例: `--status completed`。境界例: `running`は継続報告でありmanager受理待ちへ移さない
- 意味評価として残す範囲: 報告根拠が完了条件を満たすか、managerが受理すべきか

`Ctrl-C`はforegroundの配送制御だけを止める。Herdr workspaceも閉じる場合は`stop --execute`を実行する。

Herdr側でworkspaceを直接閉じてもrunの論理状態は自動停止しない。既存runの配送制御だけが終了した場合は`resume <run-id>`で再開する。`start`は既存runを再利用せず、常に別runを作る。

同じ定義から別の艦隊を増やす場合は、同じ起動コマンドをもう一度実行する。

```bash
scripts/fleet-runtime start "$FLEET" --execute
```

保存したrun状態まで削除する場合だけ`remove`を使う。

```bash
scripts/fleet-runtime remove mixed-review-0123456789abcdef0123456789abcdef --execute
```

各runは起動時の設定と実行物を固定snapshotとして保持する。モデル、command、ViewProfile、作業directoryを変更した場合は、そのYAMLから新しいrunを起動する。過去のtask状態が新しいrunへ混ざることはない。

## pane配置を変更する

`spec.view_profile`を省略すると、[既定ViewProfile](plugins/agent-fleet-herdr/adapter/config/default-view-profile.yml)を使う。独自ViewProfileは絶対パスで指定する。

```yaml
spec:
  view_profile: /absolute/path/to/view-profiles/my-layout.yml
```

ViewProfileを変更したら、起動前にもう一度`plan`を実行する。既存runの配置は変わらず、新しい設定は次に作るrunへ適用される。

## よくある失敗を直す

### Claudeが未認証と表示される

エラーに表示された`runtime.command`をFleetの外で実行し、そのCLIの公式手順に従って認証する。wrapperやaliasを指定した場合は、同じ対話shellで解決できることも確認する。

### Role Catalogが見つからない

`agent-roles`から`builtin@1.json`を書き出すか、`AGENT_ROLES_CATALOG`へ絶対パスを指定する。

### Fleet file path must be absoluteと表示される

`pwd -P`などで絶対パスを作り、その値を`plan`または`start`へ渡す。

### ViewProfileの人数制約で失敗する

既定ViewProfileではmemberを3〜7人にする。別の人数を使う場合は、対応する独自ViewProfileを`spec.view_profile`へ指定する。

## pluginを更新する

Codexでは次を実行する。

```bash
codex plugin marketplace upgrade agent-fleet
codex plugin add agent-fleet-core@agent-fleet
codex plugin add agent-fleet-herdr@agent-fleet
```

Claude Codeでは次を実行する。

```bash
claude plugin marketplace update agent-fleet
claude plugin update agent-fleet-core@agent-fleet --scope user
claude plugin update agent-fleet-herdr@agent-fleet --scope user
```

## source変更を検証する

```bash
bash scripts/validate.sh
```

保守用tool（doctor / lint-consumer-contract / evaluate-skills / release / test-hardening / validate-plugin-repository）の正本は兄弟checkoutの `../harness-tools/` であり、このrepositoryは複製を持たない。`scripts/validate.sh` は `../harness-tools/tools/` の実在を確認してから呼び、無ければ止まる。CIの `validate.yml` も `harness-tools` を兄弟checkoutして `harness-tools/ci/validate.sh` を実行する。呼び方は `../harness-tools/README.md` にある。

[意味評価fixture](evals/scenarios.json)は兄弟checkout `../harness-tools/` の評価runner（`scripts/run-evals.sh`）で生成modelと独立judgeへ渡し、入力、応答、criterionごとの逐語quoteとreasonを記録する。runnerのexit 0は全caseの記録完了だけを示し、品質承認を示さない。criterionの真偽は人またはagentが記録を再読して採否を判断するための意味証拠である。adapter非zero、不正な応答、根拠不整合など記録を完了できない操作失敗は非zeroで終了する。

## 詳細資料

- [コンセプトと設計](docs/concepts.md)
- [艦隊の連携と全体制御](docs/2026-08-31-エージェント艦隊-連携と全体制御.md)
- [作業進行ルール](docs/2026-08-31-エージェント艦隊-作業進行ルール.md)
- [非機能要件](docs/2026-08-31-エージェント艦隊-非機能要件.md)
- [役割Hookの実行契機と責務](docs/2026-09-01-エージェント艦隊-役割Hookの実行契機と責務.md)
