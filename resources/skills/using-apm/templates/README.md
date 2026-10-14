# Using the Templates

These are resource skeletons, not finished products. Read the [submission rules](../references/submission.md), copy the selected directory/file to the user's workspace, and fill in actual functionality and identity. Do not submit the entire templates/ directory as a Plugin or Agent.

| Type | Copy | CLI path |
| --- | --- | --- |
| Skill | [skill/](skill/) | Copied directory |
| Plugin | [plugin/](plugin/) | Directory containing apc-plugin.json |
| MCP stdio | [mcp-stdio.json](mcp-stdio.json) | Single JSON file |
| MCP http | [mcp-http.json](mcp-http.json) | Single JSON file |
| Agent | [agent/](agent/) | Directory containing agent.yaml |

Choose the destination before copying: a directory you intend to publish (the example below) or, for a local-only resource, the target Agent's own root. A Skill is the one type whose file layout is identical in both places — `<agent root>/skills/<name>/` takes the same `SKILL.md` and `skill-card.md` you would submit — so authoring it in place and publishing it later are both possible. MCP, Plugin and Agent resources must be placed by the Agent's own CLI or by APM, not hand-written into the Agent root; see [Where a local-only resource lives](../SKILL.md#placing-a-local-only-resource).

Example: create a publishable Skill after checking the destination does not exist, so user content is not overwritten:

```bash
cp -R /path/to/using-apm/templates/skill ./my-skill
# Edit both files with actual functionality and maintainer details.
apm validate ./my-skill --type skill --json
# Run only after the user has reviewed and explicitly approved the upload:
apm submit ./my-skill --type skill --message 'Initial submission: describe the capability'
```

Submitting is a separate, user-authorized step. See [submission.md](../references/submission.md) for the review-before-upload checklist.

Replace /path/to/using-apm with this handbook's actual location. Relative template paths are based on this file's directory.

Skill and Plugin Cards intentionally contain REPLACE_OWNER / REPLACE_CONTACT so validation fails until identity is supplied. Write the Maintainer fields as plain lines (`Owner: ...`, `Contact: ...` or `Support: ...`): a leading `- ` list marker or bold formatting makes the validator unable to read the key. Use a real person/team and working contact; do not inherit this handbook's maintainer. Update names, descriptions, versions, body instructions, inputs/outputs, dependencies, limitations and maintainer details. For Plugins, keep renamed Skill directories and apc-plugin.json declarations in sync.

Every template declares `schema_version: 2`. Submission (`apm validate` and `apm submit`) accepts only version 2 and rejects version 1 with an `upgrade to 2` error, so an older resource must be upgraded before it can be submitted. Installation still accepts version 1, which only matters for resources already published.

`name_zh`, `name_en` and `description_zh` are required on submission and are display-only metadata for the APC front end. `name` stays the call name and is NOT subject to the display-name limits. Display names are length-limited: `name_zh` is 2–16 characters (counted in Unicode characters, not bytes) and `name_en` is at most 32 printable ASCII characters with at least one letter. Never invent a display name or description: ask the user for the real wording, or report which field is missing. `agent.yaml`'s former `zh_name` key was renamed to `name_zh`; a submission still carrying `zh_name` is rejected.

MCP commands and example.com demonstrate structure only; replace them with the actual service. Choose one transport and do not merge their fields. The Agent skeleton omits an avatar: add a real compliant PNG under assets/ and agent.metadata.avatar when needed. Keep agent.id identical to agent.name: APM publishes the resource to `<bibit root>/agents/<agent.name>/` (Bibit root defaults to `~/.bianbu-agent`, overridden by `BIANBU_AGENT_HOME`) while the Runtime addresses that directory by agent.id, so a mismatched pair installs and lists correctly but cannot be found by ID. APM does not treat the mismatch as a validation error; `apm validate --type agent` reports it as a warning only, so run validate and fix it. The model/tools/skills/mcps entries are illustrative; APM passes them through unvalidated. An Agent no longer declares Hooks — it declares Plugins and the Runtime resolves the Hooks they own.

The handbook's own SKILL.md and skill-card.md contain real metadata. Embedded templates are supporting text assets, not a claim that their identity fields are complete. Format acceptance does not establish publishable functionality.
