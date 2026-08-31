#!/usr/bin/env bash
# Scenario: YAML Fleet SpecからCore stateとHerdr command-deck dry-run planを再現できる。
set -uo pipefail
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
CORE="$ROOT/plugins/agent-fleet-core"
HERDR="$ROOT/plugins/agent-fleet-herdr"
TMP_ROOT=$(mktemp -d "${TMPDIR:-/tmp}/agent-fleet-validation.XXXXXX") || exit 2
trap 'rm -rf "$TMP_ROOT"' EXIT
failed=0

for manifest in \
  "$CORE/.codex-plugin/plugin.json" "$CORE/.claude-plugin/plugin.json" \
  "$HERDR/.codex-plugin/plugin.json" "$HERDR/.claude-plugin/plugin.json"; do
  jq -e '.version=="0.1.0" and (.name=="agent-fleet-core" or .name=="agent-fleet-herdr")' "$manifest" >/dev/null || failed=1
done
jq -e '.hooks.UserPromptSubmit[0].hooks[0].type=="command" and .hooks.SessionStart[0].matcher=="startup|resume|clear|compact|fork"' \
  "$HERDR/hooks/claude-hooks.json" >/dev/null || failed=1
jq -e '.hooks.UserPromptSubmit[0].hooks[0].type=="command" and .hooks.SessionStart[0].matcher=="startup|resume|clear|compact"' \
  "$HERDR/hooks/codex-hooks.json" >/dev/null || failed=1
jq -e '.hooks=="./hooks/claude-hooks.json"' "$HERDR/.claude-plugin/plugin.json" >/dev/null || failed=1
jq -e '.hooks=="./hooks/codex-hooks.json"' "$HERDR/.codex-plugin/plugin.json" >/dev/null || failed=1
jq -e '.name=="agent-fleet" and (.plugins|length==2) and ([.plugins[].name]|sort)==["agent-fleet-core","agent-fleet-herdr"] and all(.plugins[]; .version=="0.1.0")' \
  "$ROOT/.agents/plugins/marketplace.json" "$ROOT/.claude-plugin/marketplace.json" >/dev/null || failed=1

for config in "$CORE/config/defaults.yml" "$CORE/spec/config/defaults.yml" "$HERDR/config/defaults.yml" \
  "$HERDR/view-profiles/command-deck.v1.yml" "$HERDR/adapter/schema/view-profile.schema.yml" \
  "$ROOT/configs/fleets/development-squad.yml" "$ROOT/configs/fleets/quick-review.yml" \
  "$ROOT/configs/view-profiles/development-focus.v1.yml" \
  "$ROOT/configs/view-profiles/review-grid.v1.yml"; do
  yq -e '.' "$config" >/dev/null || failed=1
done

python3 -m unittest discover -s "$CORE/spec/tests" -p 'test_*.py' >/dev/null || failed=1
python3 -m unittest discover -s "$CORE/core/tests" -p 'test_*.py' >/dev/null || failed=1
python3 -m unittest discover -s "$HERDR/adapter/tests" -p 'test_*.py' >/dev/null || failed=1

fleet_json=$(python3 -S "$CORE/spec/scripts/validate_fleet.py" \
  "$CORE/spec/examples/fleet.example.yml" --output-json) || failed=1
view_profile_json=$(yq -o=json '.' "$HERDR/view-profiles/command-deck.v1.yml") || failed=1
if [ -n "${fleet_json:-}" ]; then
  "$CORE/core/scripts/fleet-control" --db "$TMP_ROOT/core.sqlite3" \
    fleet.provision --config "$CORE/spec/examples/fleet.example.yml" \
    > "$TMP_ROOT/core.json" || failed=1
  jq -e '.ok==true and .result.members==3 and .result.tasks==3' "$TMP_ROOT/core.json" >/dev/null || failed=1

  "$CORE/core/scripts/fleet-control" --db "$TMP_ROOT/core.sqlite3" outbox \
    --fleet release-readiness --sender-ref manager-1 --target-agent-ref worker-1 \
    --type message.send --command-id validation-message \
    --payload '{"text":"role context gate"}' >/dev/null || failed=1
  "$CORE/core/scripts/fleet-control" --db "$TMP_ROOT/core.sqlite3" delivery.claim \
    --fleet release-readiness --worker-id validation-controller \
    > "$TMP_ROOT/unconfirmed-claim.json" || failed=1
  jq -e '.ok==true and .result==null' "$TMP_ROOT/unconfirmed-claim.json" >/dev/null || failed=1
  "$CORE/core/scripts/fleet-control" --db "$TMP_ROOT/core.sqlite3" context.confirm \
    --fleet release-readiness --agent-ref worker-1 --revision 1 >/dev/null || failed=1
  "$CORE/core/scripts/fleet-control" --db "$TMP_ROOT/core.sqlite3" delivery.claim \
    --fleet release-readiness --worker-id validation-controller \
    > "$TMP_ROOT/confirmed-claim.json" || failed=1
  jq -e '.ok==true and .result.command.spec.type=="message.send"' \
    "$TMP_ROOT/confirmed-claim.json" >/dev/null || failed=1

  "$HERDR/adapter/scripts/fleet-herdr" --state-db "$TMP_ROOT/herdr.sqlite3" \
    provision --fleet-json "$fleet_json" --view-profile-json "$view_profile_json" \
    --cwd "$ROOT" --agent-kind codex \
    > "$TMP_ROOT/herdr-plan.json" || failed=1
  jq -e '.ok==true and .result.mode=="dry-run" and .result.status=="planned" and (.result.plan.operations|length)==6 and (.result.plan.placements|length)==3' \
    "$TMP_ROOT/herdr-plan.json" >/dev/null || failed=1
fi

"$HERDR/adapter/scripts/fleet-runtime" list \
  --fleet-dir "$ROOT/configs/fleets" \
  --profile-dir "$ROOT/configs/view-profiles" \
  --state-dir "$TMP_ROOT/runtime-state" > "$TMP_ROOT/fleet-list.json" || failed=1
jq -e '.ok==true and (.result|length)==2 and all(.result[]; .profile_resolved==true)' \
  "$TMP_ROOT/fleet-list.json" >/dev/null || failed=1
"$HERDR/adapter/scripts/fleet-runtime" plan development-squad \
  --fleet-dir "$ROOT/configs/fleets" \
  --profile-dir "$ROOT/configs/view-profiles" \
  --state-dir "$TMP_ROOT/runtime-state" --cwd "$ROOT" --agent-kind codex \
  > "$TMP_ROOT/fleet-plan.json" || failed=1
jq -e '.ok==true and .result.status=="planned" and .result.profile_ref=="local/development-focus@1" and (.result.herdr.plan.placements|length)==5' \
  "$TMP_ROOT/fleet-plan.json" >/dev/null || failed=1
test ! -e "$TMP_ROOT/runtime-state" || failed=1

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
