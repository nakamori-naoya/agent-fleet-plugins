#!/usr/bin/env bash
# Scenario: 利用者のYAML設定からCore stateとHerdr pane配置計画を再現できる。
set -uo pipefail
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
# 保守toolの実装元は兄弟checkoutの harness-tools。無ければ止まる（fixtureで代用しない）。
TOOLS="$ROOT/../harness-tools/tools"
[ -d "$TOOLS" ] || { echo "[error] 兄弟 checkout harness-tools が無い: $TOOLS" >&2; exit 2; }
CORE="$ROOT/plugins/agent-fleet-core"
HERDR="$ROOT/plugins/agent-fleet-herdr"
ROLE_CATALOG="$ROOT/tests/fixtures/role-catalog.yml"
HOOK_PLUGIN="$HERDR/internal/agent-fleet-session-hooks"
TMP_ROOT=$(mktemp -d "${TMPDIR:-/tmp}/agent-fleet-validation.XXXXXX") || exit 2
TMP_ROOT=$(cd "$TMP_ROOT" && pwd -P) || exit 2
trap 'rm -rf "$TMP_ROOT"' EXIT
failed=0
skill_frontmatter_name() {
  awk 'NR==1 { if ($0 != "---") exit 2; next } $0=="---" { found=1; exit } { print } END { if (!found) exit 2 }' "$1" \
    | yq -er '.name | select(tag == "!!str" and length > 0)' -
}
printf '%s\n' '---' "name: 'fixture-skill' # comment" '---' 'name: body-only' > "$TMP_ROOT/frontmatter-valid.md"
printf '%s\n' '---' 'description: no name' '---' 'name: body-only' > "$TMP_ROOT/frontmatter-invalid.md"
[ "$(skill_frontmatter_name "$TMP_ROOT/frontmatter-valid.md")" = "fixture-skill" ] \
  && ! skill_frontmatter_name "$TMP_ROOT/frontmatter-invalid.md" >/dev/null 2>&1 || failed=1
python3 "$TOOLS/test-hardening.py" --repository "$ROOT" || failed=1
python3 "$TOOLS/validate-plugin-repository.py" "$ROOT" || failed=1
python3 "$TOOLS/validate-plugin-repository.py" --self-test || failed=1

for manifest in "$CORE/.codex-plugin/plugin.json" "$CORE/.claude-plugin/plugin.json"; do
  jq -e '(.version|test("^[0-9]+[.][0-9]+[.][0-9]+")) and .name=="agent-fleet-core"' "$manifest" >/dev/null || failed=1
done
for manifest in "$HERDR/.codex-plugin/plugin.json" "$HERDR/.claude-plugin/plugin.json"; do
  jq -e '(.version|test("^[0-9]+[.][0-9]+[.][0-9]+")) and .name=="agent-fleet-herdr"' "$manifest" >/dev/null || failed=1
done
for manifest in "$HOOK_PLUGIN/.codex-plugin/plugin.json" "$HOOK_PLUGIN/.claude-plugin/plugin.json"; do
  jq -e '(.version|test("^[0-9]+[.][0-9]+[.][0-9]+")) and .name=="agent-fleet-session-hooks"' "$manifest" >/dev/null || failed=1
done
jq -e '.hooks.UserPromptSubmit[0].hooks[0].type=="command" and .hooks.UserPromptSubmit[0].hooks[0].timeout==12 and .hooks.SessionStart[0].matcher=="startup|resume|clear|compact|fork" and .hooks.SessionStart[0].hooks[0].timeout==12' \
  "$HOOK_PLUGIN/hooks/claude-hooks.json" >/dev/null || failed=1
jq -e '.hooks.UserPromptSubmit[0].hooks[0].type=="command" and .hooks.UserPromptSubmit[0].hooks[0].timeout==12 and .hooks.SessionStart[0].matcher=="startup|resume|clear|compact" and .hooks.SessionStart[0].hooks[0].timeout==12' \
  "$HOOK_PLUGIN/hooks/codex-hooks.json" >/dev/null || failed=1
jq -e '.hooks.UserPromptSubmit[0].hooks[0].command=="sh" and .hooks.UserPromptSubmit[0].hooks[0].args[0]=="-c" and (.hooks.UserPromptSubmit[0].hooks[0].args[1] | contains("AGENT_FLEET_HOOK_RUNTIME") and contains("--runtime-product claude") and (contains("python3 -c")|not) and (contains("PLUGIN_ROOT")|not)) and .hooks.UserPromptSubmit[0].hooks[0].args==.hooks.SessionStart[0].hooks[0].args' \
  "$HOOK_PLUGIN/hooks/claude-hooks.json" >/dev/null || failed=1
jq -e '.hooks.UserPromptSubmit[0].hooks[0].command | contains("AGENT_FLEET_HOOK_RUNTIME") and contains("--runtime-product codex") and (contains("python3 -c")|not) and (contains("PLUGIN_ROOT")|not)' \
  "$HOOK_PLUGIN/hooks/codex-hooks.json" >/dev/null || failed=1
jq -e '.hooks.UserPromptSubmit[0].hooks[0].command==.hooks.SessionStart[0].hooks[0].command' \
  "$HOOK_PLUGIN/hooks/codex-hooks.json" >/dev/null || failed=1
jq -e 'has("hooks")|not' "$HERDR/.claude-plugin/plugin.json" >/dev/null || failed=1
jq -e '.hooks=="./internal/agent-fleet-session-hooks/hooks/codex-hooks.json" and (.interface.capabilities|index("Hooks"))!=null' "$HERDR/.codex-plugin/plugin.json" >/dev/null || failed=1
jq -e '.hooks=="./hooks/claude-hooks.json"' "$HOOK_PLUGIN/.claude-plugin/plugin.json" >/dev/null || failed=1
jq -e '.hooks=="./hooks/codex-hooks.json"' "$HOOK_PLUGIN/.codex-plugin/plugin.json" >/dev/null || failed=1
test ! -e "$HERDR/view-profiles" || failed=1
# Codex capabilityと配布物の対応（S-1で共有版へ寄せた際に失った述語を戻す）
#   基準資料: 各packageのCodex manifest（interface.capabilities、hooks）と package root直下の scripts/
#   入力: agent-fleet-core、agent-fleet-herdr、内部sidecar agent-fleet-session-hooks の3 package root
#   正規化: jqでJSONを読む。capabilitiesは文字列配列、hooksはkeyの有無
#   合格述語: (1) hooks宣言の有無 = capabilitiesに"Hooks"がある。sidecarは"Hooks"必須
#             (2) capabilitiesに"Scripts"があれば非空regular fileを持つ scripts/ がある。
#                 "Scripts"が無く"Skills"も無いpackageに scripts/ が無い
#   診断: 違反したpackage rootと述語番号
#   正例: 現状の3 package。反例: sidecarのcapabilitiesを[]にする / herdrのhooksを消す /
#         "Scripts"を足してscripts/を置かない。境界例: "Skills"を持つpackageのscripts/はtoolとして許す
#   意味評価: hook commandが艦隊sessionに適切か、capabilityの選択が利用者価値に合うか
codex_capability_contract() {
  local root=$1 sidecar=$2 manifest="$1/.codex-plugin/plugin.json"
  jq -e --argjson sidecar "$sidecar" '
    ((.interface.capabilities // []) | index("Hooks") != null) as $hooks_cap
    | (has("hooks")) as $hooks_declared
    | ($hooks_declared == $hooks_cap) and (($sidecar | not) or $hooks_cap)' "$manifest" >/dev/null \
    || { echo "capability契約(1) hooks宣言とHooks capabilityが一致しない: $root" >&2; return 1; }
  if jq -e '(.interface.capabilities // []) | index("Scripts") != null' "$manifest" >/dev/null; then
    [ -d "$root/scripts" ] && [ ! -L "$root/scripts" ] \
      && [ -n "$(find "$root/scripts" -type f -size +0 -print -quit)" ] \
      || { echo "capability契約(2) Scripts capabilityに対応するscripts/が無い: $root" >&2; return 1; }
  elif [ -d "$root/scripts" ] \
    && ! jq -e '(.interface.capabilities // []) | index("Skills") != null' "$manifest" >/dev/null; then
    echo "capability契約(2) scripts/に対応するScripts capabilityが無い: $root" >&2; return 1
  fi
}
codex_capability_contract "$CORE" false || failed=1
codex_capability_contract "$HERDR" false || failed=1
codex_capability_contract "$HOOK_PLUGIN" true || failed=1
CAP_NEG="$TMP_ROOT/capability-negatives"
mkdir -p "$CAP_NEG"
mkdir -p "$CAP_NEG/sidecar-no-hooks-cap" && cp -R "$HOOK_PLUGIN/." "$CAP_NEG/sidecar-no-hooks-cap/"
jq '.interface.capabilities=[]' "$HOOK_PLUGIN/.codex-plugin/plugin.json" > "$CAP_NEG/sidecar-no-hooks-cap/.codex-plugin/plugin.json"
codex_capability_contract "$CAP_NEG/sidecar-no-hooks-cap" true 2>/dev/null && failed=1
mkdir -p "$CAP_NEG/herdr-no-hooks/.codex-plugin"
jq 'del(.hooks)' "$HERDR/.codex-plugin/plugin.json" > "$CAP_NEG/herdr-no-hooks/.codex-plugin/plugin.json"
codex_capability_contract "$CAP_NEG/herdr-no-hooks" false 2>/dev/null && failed=1
mkdir -p "$CAP_NEG/core-scripts-cap/.codex-plugin"
jq '.interface.capabilities+=["Scripts"]' "$CORE/.codex-plugin/plugin.json" > "$CAP_NEG/core-scripts-cap/.codex-plugin/plugin.json"
codex_capability_contract "$CAP_NEG/core-scripts-cap" false 2>/dev/null && failed=1
if rg -n 'builtin_profiles|builtin/command-deck|manager_ratio' "$HERDR" >/dev/null; then
  failed=1
fi
jq -e '.name=="agent-fleet" and (.plugins|length==2) and ([.plugins[].name]|sort)==["agent-fleet-core","agent-fleet-herdr"]' \
  "$ROOT/.agents/plugins/marketplace.json" "$ROOT/.claude-plugin/marketplace.json" >/dev/null || failed=1

for config in "$CORE/config/defaults.yml" "$CORE/spec/config/defaults.yml" "$HERDR/config/defaults.yml" \
  "$HERDR/adapter/schema/view-profile.schema.yml" \
  "$ROOT/configs/fleets/development-squad.yml" "$ROOT/configs/fleets/quick-review.yml" \
  "$ROOT/configs/fleets/release-readiness.yml" \
  "$ROOT/configs/view-profiles/development-focus.v1.yml" \
  "$ROOT/configs/view-profiles/review-grid.v1.yml" \
  "$ROOT/configs/view-profiles/role-columns.v1.yml"; do
  yq -e '.' "$config" >/dev/null || failed=1
done
yq -e '.["$defs"].layoutGroup.properties.selector.additionalProperties == false and .["$defs"].layoutGroup.properties.selector.properties.role_ids.type == "array" and .["$defs"].layoutGroup.properties.selector.properties.agent_refs.type == "array" and .["$defs"].layoutGroup.properties.selector.properties.remaining.const == true' \
  "$HERDR/adapter/schema/view-profile.schema.yml" >/dev/null || failed=1

python3 -m unittest discover -s "$CORE/spec/tests" -p 'test_*.py' >/dev/null || failed=1
python3 -m unittest discover -s "$CORE/core/tests" -p 'test_*.py' >/dev/null || failed=1
python3 -m unittest discover -s "$HERDR/adapter/tests" -p 'test_*.py' >/dev/null || failed=1
python3 -m unittest discover -s "$ROOT/tests" -p 'test_*.py' >/dev/null || failed=1

fleet_json=$(python3 -S "$CORE/spec/scripts/validate_fleet.py" \
  "$ROOT/configs/fleets/development-squad.yml" \
  --role-catalog "$ROLE_CATALOG" --output-json) || failed=1
view_profile_json=$(yq -o=json '.' "$ROOT/configs/view-profiles/role-columns.v1.yml") || failed=1
if [ -n "${fleet_json:-}" ]; then
  fleet_state="$TMP_ROOT/hook-state/runs/development-squad-validation"
  hook_runtime_dir="$fleet_state/hook-runtimes/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
  fixed_core_root="$fleet_state/execution-runtimes/bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb/agent-fleet-core"
  runtime_manifest="$fleet_state/manifest.json"
  mkdir -p "$hook_runtime_dir" "$fixed_core_root" || failed=1
  cp "$HERDR/hooks/role_context.py" "$hook_runtime_dir/role_context.py" || failed=1
  cp -R "$CORE/." "$fixed_core_root/" || failed=1
  chmod 0400 "$hook_runtime_dir/role_context.py" || failed=1
  fleet_core_command="$fixed_core_root/core/scripts/fleet-control"
  fleet_core_db="$fleet_state/core.sqlite3"
  jq -n --arg core "$fleet_core_command" \
    '{runtime_commands:{core:[$core]}}' > "$runtime_manifest" || failed=1
  chmod 0600 "$runtime_manifest" || failed=1

  "$fleet_core_command" --db "$fleet_core_db" \
    fleet.provision --config "$ROOT/configs/fleets/development-squad.yml" \
    --role-catalog "$ROLE_CATALOG" \
    > "$TMP_ROOT/core.json" || failed=1
  jq -e '.ok==true and .result.members==5 and .result.tasks==4' "$TMP_ROOT/core.json" >/dev/null || failed=1

  # Two runs compiled from one definition must remain independent even when
  # they use the same logical task IDs.
  yq -o=json '.' "$ROOT/configs/fleets/development-squad.yml" \
    | jq '.metadata.id="development-squad-runone"' > "$TMP_ROOT/run-one.json" || failed=1
  yq -o=json '.' "$ROOT/configs/fleets/development-squad.yml" \
    | jq '.metadata.id="development-squad-runtwo"' > "$TMP_ROOT/run-two.json" || failed=1
  for run in one two; do
    "$CORE/core/scripts/fleet-control" --db "$TMP_ROOT/run-$run.sqlite3" \
      fleet.provision --config "$TMP_ROOT/run-$run.json" \
      --role-catalog "$ROLE_CATALOG" >/dev/null || failed=1
    "$CORE/core/scripts/fleet-control" --db "$TMP_ROOT/run-$run.sqlite3" task.assign \
      --fleet "development-squad-run$run" --task architecture-advice \
      --agent-ref advisor --manager-ref manager \
      --command-id "assign-$run" >/dev/null || failed=1
  done
  "$CORE/core/scripts/fleet-control" --db "$TMP_ROOT/run-one.sqlite3" task.report \
    --fleet development-squad-runone --task architecture-advice \
    --agent-ref advisor --status running >/dev/null || failed=1
  "$CORE/core/scripts/fleet-control" --db "$TMP_ROOT/run-one.sqlite3" task.list \
    --fleet development-squad-runone > "$TMP_ROOT/run-one-tasks.json" || failed=1
  "$CORE/core/scripts/fleet-control" --db "$TMP_ROOT/run-two.sqlite3" task.list \
    --fleet development-squad-runtwo > "$TMP_ROOT/run-two-tasks.json" || failed=1
  jq -e '.result.tasks[] | select(.task_id=="architecture-advice") | .status=="running"' \
    "$TMP_ROOT/run-one-tasks.json" >/dev/null || failed=1
  jq -e '.result.tasks[] | select(.task_id=="architecture-advice") | .status=="assigned"' \
    "$TMP_ROOT/run-two-tasks.json" >/dev/null || failed=1

  "$CORE/core/scripts/fleet-control" --db "$TMP_ROOT/deadline.sqlite3" \
    fleet.provision --config "$ROOT/configs/fleets/development-squad.yml" \
    --role-catalog "$ROLE_CATALOG" \
    >/dev/null || failed=1
  "$CORE/core/scripts/fleet-control" --db "$TMP_ROOT/deadline.sqlite3" task.assign \
    --fleet development-squad --task architecture-advice --agent-ref advisor \
    --manager-ref manager --command-id validation-architecture-assignment \
    >/dev/null || failed=1
  "$CORE/core/scripts/fleet-control" --db "$TMP_ROOT/deadline.sqlite3" task.report \
    --fleet development-squad --task architecture-advice --agent-ref advisor \
    --status running >/dev/null || failed=1
  "$CORE/core/scripts/fleet-control" --db "$TMP_ROOT/deadline.sqlite3" task.progress \
    --fleet development-squad --task architecture-advice --agent-ref advisor \
    --report-id validation-progress-1 --report '{"summary":"first"}' \
    --next-report-at '2000-01-01T00:00:00+00:00' >/dev/null || failed=1
  "$CORE/core/scripts/fleet-control" --db "$TMP_ROOT/deadline.sqlite3" task.progress \
    --fleet development-squad --task architecture-advice --agent-ref advisor \
    --report-id validation-progress-2 --report '{"summary":"second but late"}' \
    --next-report-at '2000-01-01T00:01:00+00:00' >/dev/null || failed=1
  "$CORE/core/scripts/fleet-control" --db "$TMP_ROOT/deadline.sqlite3" progress.check \
    --fleet development-squad --now '2099-01-01T00:00:00+00:00' \
    > "$TMP_ROOT/progress-check.json" || failed=1
  jq -e '.ok==true and .result.tasks[0].consecutive_missed_deadlines==2 and .result.tasks[0].requires_user_decision==true' \
    "$TMP_ROOT/progress-check.json" >/dev/null || failed=1
  "$CORE/core/scripts/fleet-control" --db "$TMP_ROOT/deadline.sqlite3" status \
    --fleet development-squad > "$TMP_ROOT/progress-status.json" || failed=1
  jq -e '.result.tasks[] | select(.task_id=="architecture-advice") | .status=="running" and .consecutive_missed_deadlines==2 and .requires_user_decision==true' \
    "$TMP_ROOT/progress-status.json" >/dev/null || failed=1
  jq -e '.result.outbox[] | select(.spec.payload.notification_type=="task.progress.user_decision_required") | .spec.target.ref=="manager"' \
    "$TMP_ROOT/progress-status.json" >/dev/null || failed=1

  "$fleet_core_command" --db "$fleet_core_db" outbox \
    --fleet development-squad --sender-ref manager --target-agent-ref worker-implementation \
    --type message.send --command-id validation-message \
    --payload '{"text":"role context gate"}' >/dev/null || failed=1
  "$fleet_core_command" --db "$fleet_core_db" delivery.claim \
    --fleet development-squad --worker-id validation-controller \
    > "$TMP_ROOT/unconfirmed-claim.json" || failed=1
  jq -e '.ok==true and .result==null' "$TMP_ROOT/unconfirmed-claim.json" >/dev/null || failed=1
  "$fleet_core_command" --db "$fleet_core_db" context.confirm \
    --fleet development-squad --agent-ref worker-implementation --revision 1 >/dev/null || failed=1
  "$fleet_core_command" --db "$fleet_core_db" delivery.claim \
    --fleet development-squad --worker-id validation-controller \
    > "$TMP_ROOT/confirmed-claim.json" || failed=1
  jq -e '.ok==true and .result.command.spec.type=="message.send"' \
    "$TMP_ROOT/confirmed-claim.json" >/dev/null || failed=1

  "$fleet_core_command" --db "$fleet_core_db" outbox \
    --fleet development-squad --sender-ref manager --target-agent-ref manager \
    --type context.sync --command-id validation-context \
    --payload '{"reason":"subprocess integration"}' >/dev/null || failed=1
  "$fleet_core_command" --db "$fleet_core_db" delivery.claim \
    --fleet development-squad --worker-id validation-hook \
    > "$TMP_ROOT/context-claim.json" || failed=1
  context_command=$(jq -c '.result.command' "$TMP_ROOT/context-claim.json") || failed=1
  context_lease=$(jq -r '.result.delivery.lease_token' "$TMP_ROOT/context-claim.json") || failed=1
  "$fleet_core_command" --db "$fleet_core_db" delivery.begin \
    --fleet development-squad --command-id validation-context \
    --lease-token "$context_lease" >/dev/null || failed=1
  context_prompt=$(printf 'AGENT_FLEET_COMMAND_V1\n%s' "$context_command")
  jq -n --arg prompt "$context_prompt" \
    '{hook_event_name:"UserPromptSubmit",session_id:"validation-session",prompt:$prompt}' \
    | env AGENT_FLEET_CORE_COMMAND="$fleet_core_command" \
      AGENT_FLEET_CORE_DB="$fleet_core_db" \
      AGENT_FLEET_SESSION_CONTEXT_DB="$TMP_ROOT/session-context.sqlite3" \
      python3 "$hook_runtime_dir/role_context.py" --runtime-product codex \
      > "$TMP_ROOT/hook-result.json" || failed=1
  jq -e '.hookSpecificOutput.additionalContext | contains("development-squad")' \
    "$TMP_ROOT/hook-result.json" >/dev/null || failed=1

  "$HERDR/adapter/scripts/fleet-herdr" --state-db "$TMP_ROOT/herdr.sqlite3" \
    provision --fleet-json "$fleet_json" \
    --view-profile-json "$view_profile_json" \
    --cwd "$ROOT" \
    > "$TMP_ROOT/herdr-plan.json" || failed=1
  jq -e '.ok==true and .result.mode=="dry-run" and .result.status=="planned" and (.result.plan.operations|length)==15 and (.result.plan.placements|length)==5' \
    "$TMP_ROOT/herdr-plan.json" >/dev/null || failed=1
fi

"$HERDR/adapter/scripts/fleet-runtime" list \
  --role-catalog "$ROLE_CATALOG" \
  --core-command "$CORE/core/scripts/fleet-control" \
  --fleet-dir "$ROOT/configs/fleets" \
  --profile-dir "$ROOT/configs/view-profiles" \
  --state-dir "$TMP_ROOT/runtime-state" > "$TMP_ROOT/fleet-list.json" || failed=1
jq -e '.ok==true and (.result|length)==3 and all(.result[]; .profile_resolved==true)' \
  "$TMP_ROOT/fleet-list.json" >/dev/null || failed=1
"$HERDR/adapter/scripts/fleet-runtime" plan "$ROOT/configs/fleets/development-squad.yml" \
  --role-catalog "$ROLE_CATALOG" \
  --core-command "$CORE/core/scripts/fleet-control" \
  --fleet-dir "$ROOT/configs/fleets" \
  --profile-dir "$ROOT/configs/view-profiles" \
  --state-dir "$TMP_ROOT/runtime-state" --cwd "$ROOT" \
  > "$TMP_ROOT/fleet-plan.json" || failed=1
jq -e '.ok==true and .result.status=="planned" and .result.profile_ref=="builtin/role-columns@1" and (.result.herdr.plan.placements|length)==5' \
  "$TMP_ROOT/fleet-plan.json" >/dev/null || failed=1
jq -e 'all(.result.herdr.plan.operations[] | select(.id=="agent.run:worker-implementation" or .id=="agent.run:worker-verification"); (.argv|join(" ")|contains("codex")) and (.argv|join(" ")|contains("your-codex-model-id")) and (.argv|join(" ")|contains("plugins.agent-fleet-herdr@agent-fleet.enabled=true")) and (.argv|join(" ")|contains("model_reasoning_effort")))' \
  "$TMP_ROOT/fleet-plan.json" >/dev/null || failed=1
jq -e 'all(.result.herdr.plan.operations[] | select(.id=="agent.run:manager" or .id=="agent.run:advisor" or .id=="agent.run:reviewer"); (.argv|join(" ")|contains("claude")) and (.argv|join(" ")|contains("your-claude-model-id")) and (.argv|join(" ")|contains("--plugin-dir")) and (.argv|join(" ")|contains("switchModelsOnFlag")))' \
  "$TMP_ROOT/fleet-plan.json" >/dev/null || failed=1
test ! -e "$TMP_ROOT/runtime-state" || failed=1

mkdir -p "$TMP_ROOT/separate/core" "$TMP_ROOT/separate/herdr"
cp -R "$CORE/." "$TMP_ROOT/separate/core/"
cp -R "$HERDR/." "$TMP_ROOT/separate/herdr/"
"$TMP_ROOT/separate/herdr/adapter/scripts/fleet-runtime" list \
  --role-catalog "$ROLE_CATALOG" \
  --core-command "$TMP_ROOT/separate/core/core/scripts/fleet-control" \
  --fleet-dir "$ROOT/configs/fleets" \
  --profile-dir "$ROOT/configs/view-profiles" \
  --state-dir "$TMP_ROOT/separate-state" > "$TMP_ROOT/separate-list.json" || failed=1
jq -e '.ok==true and (.result|length)==3' "$TMP_ROOT/separate-list.json" >/dev/null || failed=1
test ! -e "$TMP_ROOT/separate-state" || failed=1

bash -n "$CORE/core/scripts/fleet-control" || failed=1
bash -n "$HERDR/adapter/scripts/fleet-herdr" || failed=1
bash -n "$HERDR/adapter/scripts/fleet-controller" || failed=1
bash -n "$HERDR/adapter/scripts/fleet-runtime" || failed=1
test -x "$HERDR/adapter/scripts/fleet-controller" || failed=1
test -x "$HERDR/adapter/scripts/fleet-runtime" || failed=1
[ "$(skill_frontmatter_name "$CORE/skills/control-agent-fleet/SKILL.md")" = "control-agent-fleet" ] || failed=1
[ "$(skill_frontmatter_name "$HERDR/skills/provision-herdr-fleet/SKILL.md")" = "provision-herdr-fleet" ] || failed=1

if [ "$failed" -eq 0 ]; then
  echo 'Validation: passed (unit tests + dry-run integration)'
else
  echo 'Validation: failed'
fi
[ "$failed" -eq 0 ]
