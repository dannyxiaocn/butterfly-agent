---
name: model-selection
description: >
  Switch your active model or provider based on task requirements. Use when:
  a task requires deep reasoning or complex multi-step analysis (use Opus),
  you need fast, cheap responses for simple/repetitive tasks (use Haiku),
  the user asks to change model or use a specific AI model, or you want to
  optimise token cost vs quality for the current task type.
---

## How to Switch Models

Edit `core/params.json` to change your model. The change takes effect on your **next activation**.

```bash
python3 << 'EOF'
import json, pathlib, os
sid = os.environ["NUTSHELL_SESSION_ID"]
p = pathlib.Path(f"sessions/{sid}/core/params.json")
d = json.loads(p.read_text())
d["model"] = "claude-haiku-4-5-20251001"   # ← change this
p.write_text(json.dumps(d, indent=2))
print(f"Model set to: {d['model']}")
EOF
```

---

## Model Reference

| Model ID | Tier | Best for |
|----------|------|----------|
| `claude-sonnet-4-6` | **Default** | Balanced quality + speed for most tasks |
| `claude-opus-4-6` | Premium | Complex reasoning, architecture decisions, nuanced writing, long chains |
| `claude-haiku-4-5-20251001` | Fast/Cheap | Simple queries, summaries, routing, classification, high-frequency heartbeats |

To see the current model:

```bash
python3 -c "import json,pathlib,os; p=pathlib.Path(f'sessions/{os.environ[\"NUTSHELL_SESSION_ID\"]}/core/params.json'); print(json.loads(p.read_text()).get('model','default (sonnet-4-6)'))"
```

---

## Decision Guide

**Use Opus when:**
- Complex multi-step reasoning required (code architecture, research synthesis)
- Task is a one-off — cost doesn't accumulate
- User explicitly needs best quality
- You've already tried Sonnet and the output was insufficient

**Stay on Sonnet (default) when:**
- General coding, writing, or tool use
- Multi-turn conversations where cost matters
- No special quality or speed requirement

**Use Haiku when:**
- Simple classification, routing, or extraction
- High-frequency heartbeat with lightweight tasks
- Budget is a concern and task is well-defined
- Acting as a sub-agent for a simple repeated step

---

## Switching Providers

To use Kimi (MoonShot) instead of Anthropic:

```bash
python3 << 'EOF'
import json, pathlib, os
sid = os.environ["NUTSHELL_SESSION_ID"]
p = pathlib.Path(f"sessions/{sid}/core/params.json")
d = json.loads(p.read_text())
d["provider"] = "kimi"
d["model"] = "moonshot-v1-128k"
p.write_text(json.dumps(d, indent=2))
print("Switched to Kimi provider")
EOF
```

Set `provider` back to `null` (or remove the key) to return to Anthropic.

---

## Gotchas

- Changes apply **next activation**, not immediately. If the current task needs the new model right now, finish it and let the system trigger a new activation.
- Kimi does **not** support Anthropic prompt caching — switching away from Anthropic will lose caching benefits for long system prompts.
- Model names are case-sensitive and must exactly match the provider's API IDs.
