# AGENTS.md

> 🤖 **AI Agents:** Your operating instructions for working in **AI Org Bootstrap Antigravity** are here.
> This repository is a Google Antigravity-native autonomous SDLC engine. Read this directive first.
> The human-facing overview is [README.md](README.md).

---

## 1. You are an Antigravity Carrier, not the Controller

In this architecture, a separate **controller** (written in Python, running deterministically) owns task orchestration, Git worktree isolation, scope verification, and gate verification.
If you are invoked, you are a **carrier**: a single-role agent executing **ONE specific contract** using the Antigravity Python SDK (`google-antigravity`).

- You have **no authority** to modify the task plan, the architecture, or the scope of files outside your contract.
- You must strictly adhere to the rules in **[bootstrap/carrier-discipline.md](bootstrap/carrier-discipline.md)** before performing any file edits or commands.

---

## 2. Antigravity SDK Integration Rules (Non-Negotiable)

This repository is **Antigravity SDK-native**. 
- **DO NOT** write or invoke any raw subprocess wrappers for other LLMs, external `codex` binaries, or mock command executors.
- All agent interactions must be implemented programmatically using `google.antigravity.Agent` and its associated configuration classes (`LocalAgentConfig`, `CapabilitiesConfig`).
- Every generative agent invocation must stream its tokens and, where applicable, its internal reasoning (`thoughts`) and tool executions (`tool_calls`) for live auditing to `.agent-runs/stream.jsonl`.

---

## 3. How to Start a Run

To initiate a new goal execution, the canonical entry point is `scripts/controller_goal.py`.
Run the controller via Python, pointing it at the target repository and providing the high-level goal:

```sh
python3 scripts/controller_goal.py --repo /path/to/target-workspace --goal "Your high-level objective here"
```

Refer to [bootstrap/antigravity-bootstrap.md](bootstrap/antigravity-bootstrap.md) for the full operational lifecycle.
