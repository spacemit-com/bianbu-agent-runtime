# Bianbu expert schema

An expert project contains a root `agent.yaml`:

```yaml
schema_version: 2
agent:
  id: translation-expert
  version: 0.1.0
  name: TranslationExpert
  name_zh: 翻译专家
  name_en: Translation Expert
  description: Bidirectional Chinese-English translation.
  description_zh: 中英双向翻译专家
  soul: |-
    忠实翻译，不增删原意；保留术语、语气和格式。
  model:
    provider: deepseek
    name: deepseek-flash
  tools: []
  skills: []
  mcps: []
  plugins: []
  permissions: {}
  metadata:
    avatar: 译
```

Required fields are `id`, `version`, and `name`. `name` is the call name; `name_zh`,
`name_en`, `description_zh` are the display fields a client shows for its own
language, and every new record is written as `schema_version: 2` (version 1 is still
read, never written). A runnable expert also needs a model mapping with non-empty
`provider` and `name`. Version is semantic `major.minor.patch`
with an optional `v` prefix.

Selectors contain live catalog IDs. Empty means none. A single `"*"` selects every
entry of that kind and cannot be combined with named selectors. Plugin-owned
capabilities use their qualified ID, for example `acme.greeter/greet`; naming one
selects the Plugin that owns it, so the capability actually runs. Selecting the Plugin
under `plugins` is still the canonical form: it is what the Console shows, and it also
activates the Plugin's Hooks.

A declared Skill, MCP server or Plugin the device has not installed is a gap, not a
failure: the Runtime reports it in the expert's `## Power Dependencies` context and the
expert closes it through the bundled `using-apm` handbook. Declared `tools` are still
validated strictly, because they bound what the model may call. Every expert receives
`using-apm` whether or not it selects it.

Hooks are Plugin members, not an Agent field: selecting the Plugin that provides a
Hook activates it, so there is no `hooks` selector. A Plugin still provides its Hooks
as `hooks/<hook-id>/hook.json` members; `plugin.yaml` only carries identity.

## Installed form

An installed expert is one directory per expert, `agents/<ID>/agent.yaml`, holding
exactly the document above. `POST /api/v1/agents` and `PATCH /api/v1/agents/<ID>`
write it; nothing else should. The ID is generated (`com.bianbu.` plus twelve hex
digits) and never chosen by hand.

`POST` takes the whole declaration. `PATCH` is a field-level partial update: only the
fields present in the body change, `tools`/`skills`/`mcps`/`plugins` may be replaced
wholesale, and `metadata` is replaced *as a whole object* — so a client that changes
one metadata key has to read the current metadata back and merge it first, or it will
drop the keys it did not mention (for example `execution.requested_mode`).

The `.agent` artifact is a deterministic ZIP containing `agent.yaml` and optional
resources such as an avatar, produced by the development project API
(`bianbu_agent.agents.projects`: `init_agent_project`, `check_agent_project`,
`build_agent_project`). Its `runtime.yaml`, README, Git data, build output and
development Runtime state are project-only and excluded from the package. This is a
development flow, not the CLI path this Skill uses: the CLI has not exposed
`init/check/build/install` since `5fe86036` (2026-09-08).
