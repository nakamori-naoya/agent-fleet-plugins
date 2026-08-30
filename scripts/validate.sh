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
jq -e '.name=="agent-fleet" and (.plugins|length==2) and ([.plugins[].name]|sort)==["agent-fleet-core","agent-fleet-herdr"] and all(.plugins[]; .version=="0.1.0")' \
  "$ROOT/.agents/plugins/marketplace.json" "$ROOT/.claude-plugin/marketplace.json" >/dev/null || failed=1

for config in "$CORE/config/defaults.yml" "$CORE/spec/config/defaults.yml" "$HERDR/config/defaults.yml"; do
  yq -e '.' "$config" >/dev/null || failed=1
done

python3 -m unittest discover -s "$CORE/spec/tests" -p 'test_*.py' >/dev/null || failed=1
python3 -m unittest discover -s "$CORE/core/tests" -p 'test_*.py' >/dev/null || failed=1
python3 -m unittest discover -s "$HERDR/adapter/tests" -p 'test_*.py' >/dev/null || failed=1

fleet_json=$(python3 -S "$CORE/spec/scripts/validate_fleet.py" \
  "$CORE/spec/examples/fleet.example.yml" --output-json) || failed=1
if [ -n "${fleet_json:-}" ]; then
  "$CORE/core/scripts/fleet-control" --db "$TMP_ROOT/core.sqlite3" \
    fleet.provision --config "$CORE/spec/examples/fleet.example.yml" \
    > "$TMP_ROOT/core.json" || failed=1
  jq -e '.ok==true and .result.members==3 and .result.tasks==3' "$TMP_ROOT/core.json" >/dev/null || failed=1

  "$HERDR/adapter/scripts/fleet-herdr" --state-db "$TMP_ROOT/herdr.sqlite3" \
    provision --fleet-json "$fleet_json" --cwd "$ROOT" --agent-kind codex \
    > "$TMP_ROOT/herdr-plan.json" || failed=1
  jq -e '.ok==true and .result.mode=="dry-run" and .result.status=="planned" and (.result.plan.operations|length)==6 and (.result.plan.placements|length)==3' \
    "$TMP_ROOT/herdr-plan.json" >/dev/null || failed=1
fi

bash -n "$CORE/core/scripts/fleet-control" || failed=1
bash -n "$HERDR/adapter/scripts/fleet-herdr" || failed=1
rg -n '^name: control-agent-fleet$' "$CORE/SKILL.md" "$CORE/skills/control-agent-fleet/SKILL.md" >/dev/null || failed=1
rg -n '^name: provision-herdr-fleet$' "$HERDR/SKILL.md" "$HERDR/skills/provision-herdr-fleet/SKILL.md" >/dev/null || failed=1

if [ "$failed" -eq 0 ]; then
  echo 'Validation: passed (44 unit tests + dry-run integration)'
else
  echo 'Validation: failed'
fi
[ "$failed" -eq 0 ]
