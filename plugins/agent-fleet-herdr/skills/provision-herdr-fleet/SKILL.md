---
name: provision-herdr-fleet
description: 検査済みFleet JSONをHerdr 0.8のRuntime/View操作へ変換し、logical agentへcommandを配送する。
---

# provision-herdr-fleet

このentryは配布形式を中立化する薄い入口である。plugin rootの正本`SKILL.md`を全文読んで従う。

```bash
AGENT_FLEET_PLUGIN_ROOT="${PLUGIN_ROOT:-${CLAUDE_PLUGIN_ROOT:-/absolute/path/to/agent-fleet-herdr}}"
test -f "${AGENT_FLEET_PLUGIN_ROOT}/SKILL.md" || exit 2
cat "${AGENT_FLEET_PLUGIN_ROOT}/SKILL.md"
```
