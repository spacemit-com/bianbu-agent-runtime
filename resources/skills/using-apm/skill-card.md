---
schema_version: 2
---
# Skill Card: APM Agent Handbook

## Capability

Guide agents through discovering, installing and maintaining Bianbu/SpaceMIT Powers using APM CLI, creating, validating, repairing and submitting all four resource types in Power format v2, and letting a running Bibit expert provision the Skills or MCPs its own identity declares but its session is missing.

## Use Cases

Install community Powers, create Skills/Plugins/MCPs/Agents, resolve submission validation failures, handle missing API Keys, or close a capability gap that a running Bibit expert detects in its own session.

## Inputs and Outputs

Input: User intent, Power name/type/version, target Agent, chosen endpoint (local only or publish) or local resource path; for self-provisioning also the expert's own `agent.yaml` declarations and the capability list the Runtime gave the session. The target Agent's root directory (`~/.codex`, `~/.claude`, `~/.hermes` or `~/.bianbu-agent`) is the inspection point, since APM places Skills at `<root>/skills/<name>`, filesystem Plugins at `<root>/plugins/<name>` and Bibit Agents at `<root>/agents/<name>`; it is also where a local-only Skill is authored, while a publishable resource stays in the workspace.
Output: CLI query and installation results, validatable resource directories/files, a locally placed resource when the user chose local only, repaired validation errors, and submission IDs with actual status.

## Dependencies

APM (Agent Power Manager) CLI. Self-provisioning requires a Bibit expert with its own `agent.yaml` and targets `--agent bibit` only; it does not apply to codex, claude-code or hermes-agent. Cloud operations require networking; submit and submission queries require APC_API_KEY in the execution environment. Local validate requires neither networking nor a Key.

## Limitations

All Power operations use APM CLI. This guide cannot obtain account credentials or guarantee review approval. Templates require real content; MCP format validation does not prove the service works. Check installed CLI help. Submission accepts only Power format v2 and requires the user's explicit approval before uploading. Display fields (`name_zh`, `name_en`, `description_zh`) must come from the user; this guide does not invent or machine-translate them, and it enforces their length limits (`name_zh` 2-16 characters, `name_en` at most 32 printable ASCII characters). A local-only resource is placed under the target Agent's root through that Agent's own layout and CLI; only a Skill uses the same file layout in both the workspace and the Agent root, so MCP, Plugin and Agent resources are not hand-placed there. Self-provisioning requires a confirmed gap before installing, and defers self-inferred needs, Plugin-type Powers and ambiguous matches to the user.

## Maintainer

Owner: liangjunzhao
Contact: junzhao.liang@spacemit.com
