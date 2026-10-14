---
schema_version: 2
name: expert-creator
name_zh: 专家创建器
name_en: Expert Creator
description: Use when a user asks in natural language to create, configure, package, install, or update a Bianbu expert or Agent.
description_zh: 当用户用自然语言要求创建、配置、打包、安装或更新 Bianbu 专家（Agent）时使用。
---

# Expert Creator

Create or update one installed Bianbu expert when the request says to create or
configure it. An expert's behavior belongs in `soul`; `description` is only its
user-facing summary. The Runtime owns expert identity: it generates the
`com.bianbu.<hex>` ID and writes `agents/<ID>/agent.yaml`, so never invent an ID
or write the data directory by hand.

## Resolve the request

Derive a call `name`, display names (`name_zh`, `name_en`), summaries
(`description`, `description_zh`), specific `soul`, and avatar. Preserve explicit user choices. Use empty capability lists by
default and add only Tools, Skills, MCP servers, or Plugins required for the role;
`*` means unrestricted access and is never a convenience default. A Tool, Skill or MCP
server that lives inside a Plugin is a member of that Plugin: select it with
`--plugin <plugin-id>`, not `--skill <plugin-id>/<skill-id>`. Naming the member also
works (the Runtime then selects its Plugin), but the Plugin is what the Console and the
declaration show.

Every expert needs a concrete provider and model. When the user did not choose them:

1. Query the running Runtime's `/api/v1/providers` and `/api/v1/agents` resources;
   never read or print credential files. The local Runtime listens on
   `http://127.0.0.1:9899` unless `$BIANBU_RUNTIME_BASE_URL` says otherwise.
2. Prefer the current default Agent's model only when its provider reports
   `configured: true` and the Agent reports `available: true`.
3. Otherwise use provider model discovery. If no usable pair exists, report the
   provider error instead of creating an expert that cannot run. If several materially
   different choices remain, ask the user rather than guessing cost or capability.

## Create and publish

Run the bundled script from this Skill directory with `--name`, `--soul`,
`--provider`, and `--model`; add display fields and one flag per required capability:

```bash
python3 scripts/create_expert.py \
  --name TranslationExpert --name-zh 翻译专家 --name-en Translation Expert \
  --description "Bidirectional Chinese-English translation." \
  --description-zh "中英双向翻译专家" \
  --soul "忠实翻译，不增删原意；保留术语、语气和格式。" \
  --provider <provider> --model <model> \
  [--tool <tool-id>] [--skill <skill-id>] [--mcp <mcp-id>] [--plugin <plugin-id>] \
  [--avatar 译]
```

`name` is the call name; `name_zh` and `name_en` are the display names the
Console and Bibit show for their own language, and `description_zh` is the
Chinese summary. Never invent display text: ask the user for the wording, or
report the field that is still missing.

It prints one JSON summary, for example
`{"expert_id": "com.bianbu.1a2b3c4d5e6f", "name": "TranslationExpert",
"available": true, "unavailable_reason": "", "missing_powers": []}`.

Creating the declaration *is* installing it: `POST /api/v1/agents` stores the
expert, registers it with Security and activates its Worker. There is no separate
build or install step, and the list below no longer exists in the CLI — it was
removed in `5fe86036` (2026-09-08), when the CLI became resource-only:

```text
bianbu-agent init/check/build/install/run/stop/logs   # 已移除，不要调用
```

Read the expert back before reporting success:

```bash
bianbu-agent --config ~/.bianbu-agent/config.yaml agent show <EXPERT_ID>
bianbu-agent --config ~/.bianbu-agent/config.yaml agent verify <EXPERT_ID>
```

Stop with the Runtime's own message when the request is rejected, or when the
created expert reports `available: false` or a non-empty `unavailable_reason`. A
non-empty `missing_powers` is a gap, not a failure: the Runtime exposes it in the
expert's `## Power Dependencies` context and the expert closes it through the
bundled `using-apm` handbook.

Updating an existing expert needs the user to name it: read it with `agent show`,
then re-run the script with `--update <EXPERT_ID>` and *only* the fields the user
asked to change:

```bash
python3 scripts/create_expert.py --update <EXPERT_ID> --soul "改过的 soul。"
```

`--update` is a partial update: every flag you leave out keeps its stored value, so
a soul edit cannot silently reset the Avatar, the model, or the capabilities the
user never mentioned. Pass `--provider` and `--model` together or not at all. The
script refuses to create — or rename someone onto — a second expert with the same
`name`/`name_zh`/`name_en`; never replace one silently. A picture avatar is a separate
`POST /api/v1/agents/<EXPERT_ID>/avatar` upload, only when the user actually
supplied an image file.

Read [references/agent-schema.md](references/agent-schema.md) before adding structured
permissions, package resources, or updating an existing expert.

## Boundaries

- Installed data stays per-user under `~/.bianbu-agent`, even when the service
  itself is system-managed. The `*.agent` project flow (`bianbu_agent.agents.projects`)
  is a development API, not this skill's installation path.
- Do not copy API keys into `agent.yaml`; it stores only `provider` and model `name`.
- Never overwrite an installed expert or silently create a duplicate name.
- Report successful creation only after `agent show`/`agent verify` read the expert
  back and the creation response reported `available: true`.
