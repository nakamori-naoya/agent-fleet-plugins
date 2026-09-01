---
name: control-agent-fleet
description: YAML Fleet Specを検査し、agent fleetのlogical state、task、command、eventを管理する。pane操作は行わない。
---

# control-agent-fleet

このentryは配布形式を中立化する薄い入口である。plugin rootの正本`SKILL.md`を全文読んで従う。

```bash
AGENT_FLEET_PLUGIN_ROOT="${PLUGIN_ROOT:-${CLAUDE_PLUGIN_ROOT:-/absolute/path/to/agent-fleet-core}}"
test -f "${AGENT_FLEET_PLUGIN_ROOT}/SKILL.md" || exit 2
cat "${AGENT_FLEET_PLUGIN_ROOT}/SKILL.md"
```
