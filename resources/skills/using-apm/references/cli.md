# APM CLI Reference

## Availability and configuration

```bash
command -v apm
apm --help
```

The help identifies Agent Power Manager and includes search/install/validate/submit/submission. Do not confuse it with another package manager named APM. If missing, check whether the system repositories offer `agent-power-manager` (`apt-cache policy agent-power-manager` on Bianbu/Debian). Install a trusted candidate through the system package manager when dependency installation is within the user's task. A user-provided official APM release package is another option. Do not invent download URLs, run unknown installer scripts or require this source repository. If no source is available, report the missing CLI and continue preparing local resources.

Run APM as the target user; do not blindly use `sudo apm` to resolve permissions. `APM_CLOUD_PROVIDER` defaults to `remote`; confirm the configured `APC_BASE_URL` rather than assuming an environment (the APM default moved to production on 2026-09-24; the test environment is `https://developer.bianbu.xyz`). Respect the configured service. Do not switch providers, change addresses or disable TLS validation to evade authentication or errors. The local provider is for explicit test fixtures only.

## Commands

Angle brackets denote values to replace; square brackets denote optional syntax. Do not type either literally. Check actual `apm <command> --help`; never invent unsupported flags.

| Operation | Command |
| --- | --- |
| Search all types | `apm search <keyword> --json` |
| Search one type | `apm search <keyword> --type skill --json` |
| Inspect details | `apm show <name>[=v1.2.0] --type skill --json` |
| List cloud Powers | `apm list --type skill --json` |
| List all versions of a name | `apm list <name> --all --type skill --json` |
| Inspect installed records | `apm list <name> --installed --agent codex --type skill --json` |
| Install an exact version | `apm install <name>=v1.2.0 --type skill --agent codex` |
| Install the latest catalog version | `apm install <name> --type skill --agent codex` |
| Uninstall | `apm uninstall <name> --type skill --agent codex` |
| Recover an interrupted transaction | `apm fix` |
| Validate offline | `apm validate <path> --type skill --json` |
| Submit | `apm submit <path> --type skill --message 'Describe this change'` |
| Inspect a submission | `apm submission get <submission-id> --output json` |
| List active submissions | `apm submission list --type skill --name <name> --output json` |

Types are skill/plugin/mcp/agent. validate and submit require `--type`. Both accept only Power resource format v2 (`schema_version: 2`); a version 1 resource is rejected with an `upgrade to 2` error. Installation still accepts version 1, so already-published resources remain installable. explicitly supply the resolved type when installing to avoid name ambiguity. search accepts one nonempty keyword, without `--publisher`. Use the returned canonical name, without a `bianbu/spacemit/` prefix. Version input uses `=v1.2.0`, not `@v1.2.0` or `--version`.

Community distribution does not prove official authorship. search/show currently have no publisher or official-verification field. If the user requires official authorship, name/description/tags are insufficient evidence: explain the CLI limitation and request an explicit official resource identity or source evidence. Do not bypass the CLI to query service endpoints.

`--json` applies to search/list/show/validate; submission get/list use `--output json`. install/uninstall/submit/fix have no JSON output flag. Global `--verbose` enables diagnostics; inspect output for sensitive information before sharing.

list queries the cloud unless `--installed` is supplied. Its `--agent` flag requires `--installed`. `list <name> --all` lists cloud versions, not all Agents or extra metadata; it does not add the localized display fields. uninstall accepts no version.

`name_zh`, `name_en` and `description_zh` are submission-time metadata stored in the resource files; they are not a `list`/`show` output field in this release, and `search` matches `name` only, never a display name. Read them from the resource file you are preparing, not from catalog output. Display names are length-limited on submission: `name_zh` is 2–16 characters and `name_en` is at most 32 printable ASCII characters with at least one letter; canonical `name` is not subject to those limits. `description` and `description_zh` are both capped at 500 UTF-16 code units, which is APC's own limit rather than a byte count.

## Target Agents and installation results

| Agent ID | Skill / Plugin / MCP | Agent-type Power |
| --- | --- | --- |
| `codex` | Supported | Unsupported |
| `claude-code` | Supported | Unsupported |
| `hermes-agent` | Supported | Unsupported |
| `bibit` | Supported | Supported |

Installing/removing skill/plugin/mcp requires `--agent`. Use `--agent all` only when the user requests all installed Agents, and inspect every result. With `--type agent`, the target defaults to bibit; explicit `--agent bibit` is valid, but all is not.

For installed lists of the first three types, omitting `--agent` includes all supported Agents; Agent-type lists default to Bibit. If a target is absent, handle the CLI error rather than creating an empty profile to impersonate an installed Agent.

## Agent roots and where Powers land

APM considers an Agent installed when its root directory exists, which is why a wrong or missing root makes `--agent <id>` fail with `unsupported or not installed` instead of installing. The root is also where you inspect the result after installing. Creating and validating a resource does not need any Agent root — `apm validate` is offline — so you can author for an Agent that is not present on this machine and install it later.

| Agent ID | Agent CLI | Default root | Root override |
| --- | --- | --- | --- |
| `codex` | `codex` | `~/.codex` | `CODEX_HOME` |
| `claude-code` | `claude` | `~/.claude` | `CLAUDE_CONFIG_DIR` |
| `hermes-agent` | `hermes` | `~/.hermes` | `HERMES_HOME` |
| `bibit` | `bianbu-agent` | `~/.bianbu-agent` | `BIANBU_AGENT_HOME` |

Where APM puts a Power, relative to that Agent root (`<name>` is the canonical Power name declared in the resource file, not the local folder you author in):

| Power type | Landing | Applies to |
| --- | --- | --- |
| Skill | `<root>/skills/<name>/` | all four Agents |
| Plugin | `<root>/plugins/<name>/` | `bibit`, `hermes-agent` |
| Plugin | Registered as a local marketplace named `apm-<name>`; the Agent CLI then installs it and owns the installed copy | `codex`, `claude-code` |
| Agent | `<root>/agents/<name>/` | `bibit` only |

MCP is not landed by APM. APM calls the target Agent's own CLI (`codex mcp add`, `claude mcp add`, `hermes mcp add`, `bianbu-agent mcp add`) and that CLI stores the server in its own configuration. For Bibit the definition ends up under the runtime `data_dir` as `<data_dir>/mcps/mcp.<name>/mcp.json`; `data_dir` comes from `--config`, then `$PWD/runtime.yaml`, then `BIANBU_AGENT_CONFIG`, then `~/.bianbu-agent/config.yaml`, so it is not necessarily the `BIANBU_AGENT_HOME` root.

APM's own state is separate from every Agent root. The state root defaults to `~/.apm` and holds the catalog, the per-Agent install manifests at `manifests/<id>.json` and transaction journals; the cache defaults to `~/.cache/apm` and holds downloaded and staged content. Both follow APM's own environment: `APM_HOME` replaces the home used for the state root (so state moves to `$APM_HOME/.apm`), and `XDG_CACHE_HOME` moves the cache to `$XDG_CACHE_HOME/apm`. Authoring a resource needs none of them; they only help you inspect what APM recorded.

Treat these paths as APM's current behavior for locating and inspecting results, not as a resource-format requirement. The CLI stays the authority for what actually happened: check the exit code and the `<agent>/<type>/<name>@<version> <outcome>` line, then confirm with `apm list <name> --installed --agent <id> --type <type> --json`.

An Agent's root holds both what APM installed and what that Agent accepts on its own, and the two are not always the same file. APM's install path is the only writer of its manifests; a resource you place by hand is invisible to `apm list --installed`, and the Agent's own CLI can load a resource APM never recorded. When the user wants a resource to work locally without publishing, follow that Agent's own layout and registration command — see [Placing a local-only resource](../SKILL.md#placing-a-local-only-resource).

Example: the user requests vision-helper v1.2.0 for Codex, and search confirms it is a skill:

```bash
apm search vision-helper --json
apm show vision-helper=v1.2.0 --type skill --json
apm install vision-helper=v1.2.0 --type skill --agent codex
apm list vision-helper --installed --type skill --agent codex --json
```

vision-helper is an illustrative name, not a guaranteed catalog entry. Installation output uses `<agent>/<type>/<name>@<version> <outcome>`; the output's @ is not input syntax. Check exit codes and outcomes; partial batch success is not overall success. An already installed exact version may return unchanged. Update through install with the requested version; do not invent `apm update`. `apm fix` recovers interrupted transactions, not submission source files. Preserve evidence on identity conflicts or lock contention; do not delete manifests or forcibly unlock.

## Authentication

submit and submission get/list require `APC_API_KEY`. Remote resource reads for search/list/show/install do not require a submission Key; validate is completely offline. Submitting is a separate step that requires the user's explicit approval of the review summary first — see [submission rules](submission.md#completion-and-upload-approval).

On `submission requires APC_API_KEY` or `requires API key`, tell the user:

> Sign in to the SpaceMIT community, obtain an API Key, and configure APC_API_KEY in the environment that actually runs APM. Then retry the submission. Do not send the Key in chat; offline validation can still be completed.

The default community service entry is [developer.bianbu.xyz](https://developer.bianbu.xyz). Ask the user to locate API Key management in their community account. This guide does not assume a specific menu or Key-page URL; contact community support if it cannot be found. The CLI has no `apm login`, `apm auth login` or Key-writing command.

The user can configure a Bash session without echoing the Key:

```bash
set +x
read -r -s -p 'APC API Key: ' apm_community_key
printf '\n'
export APC_API_KEY="$apm_community_key"
unset apm_community_key
```

Run the original apm command in that terminal, or start the Agent from that environment. Exporting in another terminal does not change an already running Agent's environment. Client credential/environment settings must reach the process executing APM. Do not print the variable, scan other applications' credentials, or write the Key into templates, submission messages or resource directories.

For invalid/expired Keys or permission errors, ask the user to update the Key or check account permissions in the community. Do not blindly retry or switch to direct service requests. With no Key, run standalone `apm validate`: submit can be rejected by the Key check before source preparation.

## Submission tracking and retries

submit requires a nonempty `--message`. This message belongs to the submission, not resource metadata. Output includes outcome, submission_id, status and a CLI tracking command. Save the ID and use get even after it disappears from the active list.

| Status | Meaning and action |
| --- | --- |
| received / validating | Received / being validated; not published |
| validation_failed | Inspect validation_errors, repair sources, validate and resubmit |
| review_pending | Awaiting review; report the state |
| rejected | Inspect review_comment, revise and resubmit |
| approved / publishing | Review approved / publishing; not finished |
| publish_failed | Preserve the ID; query again or contact community support |
| published | Report published_version; verify through show/list when needed |

`submission list` returns only the current Key's active submissions, at most twenty, not full history. `--status` accepts only received, validating, review_pending, approved and publishing. Use get with a known ID for failed or published submissions.

A timeout or disconnected upload does not prove the server received nothing: inspect submission list/get first. Each new CLI submit generates a new idempotency key; rerunning is not guaranteed to repeat the same logical request. On active-submission quota errors, list first. The CLI has no cancel, review or publication-retry command. If continued monitoring is requested, query periodically through the CLI, avoid tight polling and report the actual state.
