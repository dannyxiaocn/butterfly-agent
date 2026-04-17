# `agenthub/agent/` Changelog

## v1.1.0 — 2025-03-26

Improved all prompts based on context engineering and agent prompting best practices research.

### system.md
- Added `<core_behaviors>` block with explicit behavioral directives (step-by-step thinking, honesty about uncertainty, conciseness, default-to-action, parallel tool use)
- Restructured using XML tags (`<core_behaviors>`, `<tool_creation>`) for unambiguous parsing per Anthropic guidelines
- Replaced vague examples list with concise capability summary — less token waste, same information
- Added "default to action" directive: implement changes rather than only suggesting them
- Added parallel tool calling guidance
- Tightened prose throughout: ~30% fewer tokens with same or better clarity

### task.md (renamed from heartbeat.md)
- Wrapped task injection in `<current_tasks>` XML tags for clearer context boundaries
- Added priority directive: "Focus on the highest-priority incomplete item"
- Wrapped post-activation instructions in `<after_this_activation>` tags
- Simplified path in clear-board command (removed `sessions/YOUR_ID/` prefix — bash default workdir is already the session dir)
- Removed redundant `SESSION_FINISHED` explanation paragraph — the inline instruction is sufficient
- ~40% fewer tokens

### Research sources
- Anthropic "Building Effective Agents" (Dec 2024): simple composable patterns > complex frameworks
- Anthropic "Prompting Best Practices" (Claude 4.6): XML tags, role clarity, default-to-action, parallel tool use
- Andrej Karpathy / Tobi Lutke on "Context Engineering": right information, right format, right time
- Phil Schmid "Context Engineering" (Jun 2025): agent failures are context failures, not model failures
