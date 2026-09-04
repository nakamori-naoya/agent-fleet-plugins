#!/usr/bin/env bash
# Scenario: 利用者のYAML設定からCore stateとHerdr pane配置計画を再現できる。
set -uo pipefail
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
CORE="$ROOT/plugins/agent-fleet-core"
HERDR="$ROOT/plugins/agent-fleet-herdr"
ROLE_CATALOG="$ROOT/tests/fixtures/role-catalog.yml"
HOOK_PLUGIN="$HERDR/session-hooks-plugin"
TMP_ROOT=$(mktemp -d "${TMPDIR:-/tmp}/agent-fleet-validation.XXXXXX") || exit 2
TMP_ROOT=$(cd "$TMP_ROOT" && pwd -P) || exit 2
trap 'rm -rf "$TMP_ROOT"' EXIT
failed=0

python3 "$ROOT/scripts/validate-distribution.py" "$ROOT" || failed=1
python3 "$ROOT/scripts/validate-distribution.py" --self-test || failed=1

for manifest in \
  "$CORE/.codex-plugin/plugin.json" "$CORE/.claude-plugin/plugin.json" \
  "$HERDR/.codex-plugin/plugin.json" "$HERDR/.claude-plugin/plugin.json"; do
  jq -e '.version=="0.6.1" and (.name=="agent-fleet-core" or .name=="agent-fleet-herdr")' "$manifest" >/dev/null || failed=1
done
for manifest in "$HOOK_PLUGIN/.codex-plugin/plugin.json" "$HOOK_PLUGIN/.claude-plugin/plugin.json"; do
  jq -e '.version=="0.6.1" and .name=="agent-fleet-session-hooks"' "$manifest" >/dev/null || failed=1
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
jq -e 'has("hooks")|not' "$HERDR/.claude-plugin/plugin.json" "$HERDR/.codex-plugin/plugin.json" >/dev/null || failed=1
jq -e '.hooks=="./hooks/claude-hooks.json"' "$HOOK_PLUGIN/.claude-plugin/plugin.json" >/dev/null || failed=1
jq -e '.hooks=="./hooks/codex-hooks.json"' "$HOOK_PLUGIN/.codex-plugin/plugin.json" >/dev/null || failed=1
test ! -e "$HERDR/view-profiles" || failed=1
if rg -n 'builtin_profiles|builtin/command-deck|manager_ratio' "$HERDR" >/dev/null; then
  failed=1
fi
jq -e '.name=="agent-fleet" and (.plugins|length==3) and ([.plugins[].name]|sort)==["agent-fleet-core","agent-fleet-herdr","agent-fleet-session-hooks"] and all(.plugins[]; .version=="0.6.1")' \
  "$ROOT/.agents/plugins/marketplace.json" "$ROOT/.claude-plugin/marketplace.json" >/dev/null || failed=1

for config in "$CORE/config/defaults.yml" "$CORE/spec/config/defaults.yml" "$HERDR/config/defaults.yml" \
  "$HERDR/adapter/schema/launch-profile.schema.yml" \
  "$HERDR/adapter/schema/view-profile.schema.yml" \
  "$ROOT/configs/fleets/development-squad.yml" "$ROOT/configs/fleets/quick-review.yml" \
  "$ROOT/configs/fleets/release-readiness.yml" \
  "$ROOT/configs/herdr-launch-profiles/development-squad.yml" \
  "$ROOT/configs/herdr-launch-profiles/quick-review.yml" \
  "$ROOT/configs/herdr-launch-profiles/release-readiness.yml" \
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
launch_profile_json=$(yq -o=json '.' "$ROOT/configs/herdr-launch-profiles/development-squad.yml") || failed=1
view_profile_json=$(yq -o=json '.' "$ROOT/configs/view-profiles/role-columns.v1.yml") || failed=1
if [ -n "${fleet_json:-}" ]; then
  fleet_state="$TMP_ROOT/hook-state/fleets/development-squad"
  hook_runtime_dir="$fleet_state/hook-runtimes/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
  fixed_core_root="$fleet_state/execution-runtimes/bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb/agent-fleet-core"
  runtime_manifest="$TMP_ROOT/hook-state/runtimes/development-squad.json"
  mkdir -p "$hook_runtime_dir" "$fixed_core_root" "$(dirname "$runtime_manifest")" || failed=1
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
    provision --fleet-json "$fleet_json" --launch-profile-json "$launch_profile_json" \
    --view-profile-json "$view_profile_json" \
    --cwd "$ROOT" \
    > "$TMP_ROOT/herdr-plan.json" || failed=1
  jq -e '.ok==true and .result.mode=="dry-run" and .result.status=="planned" and (.result.plan.operations|length)==10 and (.result.plan.placements|length)==5' \
    "$TMP_ROOT/herdr-plan.json" >/dev/null || failed=1
fi

"$HERDR/adapter/scripts/fleet-runtime" list \
  --role-catalog "$ROLE_CATALOG" \
  --core-command "$CORE/core/scripts/fleet-control" \
  --fleet-dir "$ROOT/configs/fleets" \
  --launch-dir "$ROOT/configs/herdr-launch-profiles" \
  --profile-dir "$ROOT/configs/view-profiles" \
  --state-dir "$TMP_ROOT/runtime-state" > "$TMP_ROOT/fleet-list.json" || failed=1
jq -e '.ok==true and (.result|length)==3 and all(.result[]; .profile_resolved==true)' \
  "$TMP_ROOT/fleet-list.json" >/dev/null || failed=1
"$HERDR/adapter/scripts/fleet-runtime" plan development-squad \
  --role-catalog "$ROLE_CATALOG" \
  --core-command "$CORE/core/scripts/fleet-control" \
  --fleet-dir "$ROOT/configs/fleets" \
  --launch-dir "$ROOT/configs/herdr-launch-profiles" \
  --profile-dir "$ROOT/configs/view-profiles" \
  --state-dir "$TMP_ROOT/runtime-state" --cwd "$ROOT" \
  > "$TMP_ROOT/fleet-plan.json" || failed=1
jq -e '.ok==true and .result.status=="planned" and .result.profile_ref=="local/role-columns@1" and (.result.herdr.plan.placements|length)==5' \
  "$TMP_ROOT/fleet-plan.json" >/dev/null || failed=1
jq -e 'all(.result.herdr.plan.operations[] | select(.id=="agent.start:worker-implementation" or .id=="agent.start:worker-verification"); (.argv|index("codex")) != null and (.argv|index("gpt-5.6-sol")) != null and (.argv|index("plugins.agent-fleet-session-hooks@agent-fleet.enabled=true")) != null and (.argv|index("model_reasoning_effort=\"medium\"")) != null)' \
  "$TMP_ROOT/fleet-plan.json" >/dev/null || failed=1
jq -e 'all(.result.herdr.plan.operations[] | select(.id=="agent.start:manager" or .id=="agent.start:advisor" or .id=="agent.start:reviewer"); (.argv|index("claude")) != null and (.argv|index("claude-fable-5-1")) != null and (.argv|index("--plugin-dir")) != null and (.argv|index("high")) != null and (.argv|index("{\"switchModelsOnFlag\":false}")) != null)' \
  "$TMP_ROOT/fleet-plan.json" >/dev/null || failed=1
test ! -e "$TMP_ROOT/runtime-state" || failed=1

mkdir -p "$TMP_ROOT/separate/core" "$TMP_ROOT/separate/herdr"
cp -R "$CORE/." "$TMP_ROOT/separate/core/"
cp -R "$HERDR/." "$TMP_ROOT/separate/herdr/"
"$TMP_ROOT/separate/herdr/adapter/scripts/fleet-runtime" list \
  --role-catalog "$ROLE_CATALOG" \
  --core-command "$TMP_ROOT/separate/core/core/scripts/fleet-control" \
  --fleet-dir "$ROOT/configs/fleets" \
  --launch-dir "$ROOT/configs/herdr-launch-profiles" \
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
rg -n '^name: control-agent-fleet$' "$CORE/SKILL.md" "$CORE/skills/control-agent-fleet/SKILL.md" >/dev/null || failed=1
rg -n '^name: provision-herdr-fleet$' "$HERDR/SKILL.md" "$HERDR/skills/provision-herdr-fleet/SKILL.md" >/dev/null || failed=1

if [ "$failed" -eq 0 ]; then
  echo 'Validation: passed (unit tests + dry-run integration)'
else
  echo 'Validation: failed'
fi
[ "$failed" -eq 0 ]
