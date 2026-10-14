# Power Submission Validation and Repair

Use the bundled [templates](../templates/README.md) and actual `apm validate` results as the format authority. This reference was checked against APM source on 2026-09-28 (Power resource format v2); confirm installed capabilities through CLI help. All four types obtain metadata from resource files, not submit name/version override flags.

## Shared constraints

| Item | Requirement |
| --- | --- |
| schema_version | Integer `2` in every authoritative file. Submission accepts only 2; version 1 is rejected with `upgrade to 2` (installation still accepts 1, which only matters for already-published resources) |
| name | 1–64 bytes; lowercase a-z, digits and hyphens; no leading/trailing or consecutive hyphens. `name` is the call name and the sort key; the display-name length limits below do NOT apply to it |
| name_zh | Required on submission; display-only for the APC front end; nonempty after trimming; 2–16 Unicode characters (counted in characters, not bytes); no control characters; free text, not a canonical name |
| name_en | Required on submission; display-only; nonempty after trimming; at most 32 characters; every character must be printable ASCII (`0x20`–`0x7e`) with at least one ASCII letter; spaces, case and ordinary punctuation allowed |
| description_zh | Required on submission; display-only; nonempty after trimming; at most 500 UTF-16 code units; newlines allowed |
| version | Strict `vMAJOR.MINOR.PATCH`, e.g. `v1.0.0`; no leading zeros, prerelease or build metadata |
| description | Nonempty after trimming, and at most 500 UTF-16 code units; describe the actual capability |
| Source directories | At most 100 MiB total, 1000 files, 10 MiB per file, depth 16, relative path length 255 bytes |
| Physical files | Regular files/directories; no symbolic/hard links or devices/FIFOs/sockets; files must not change while read |
| Skill/Plugin content | Safe UTF-8 text throughout; no binaries, nested archives, NUL or disallowed control characters |
| MCP | One complete JSON file, at most 10 MiB, checked against the definition schema |
| Agent | Safe text in agent.yaml; ordinary binary assets allowed; all files reject executable permission bits and nested archives, checked by extension and signature |

Identify conflicting files and their purpose before repairing them. Do not delete arbitrary data or disguise binaries by renaming extensions. Keep .git, build output, virtual environments, test records and private keys outside resource roots.

## Skill

The root requires `SKILL.md` and `skill-card.md`. SKILL.md starts at its first byte with a standalone `---` YAML frontmatter opener, without BOM or leading blank lines. The frontmatter is a mapping without duplicate keys and must carry integer `schema_version: 2` plus nonempty strings for name/name_zh/name_en/version/description/description_zh. Explain triggers, operations and limitations in the body; make the description about when the skill applies so agents can discover it.

skill-card.md also starts with frontmatter containing integer `schema_version: 2`; its structure is unchanged from version 1, and the version bump only keeps both files of one resource consistent. Its closing marker is immediately followed by `# Skill Card: <display name>`, with no blank line between them. These six level-two headings must match exactly and each have nonempty content:

- Capability
- Use Cases
- Inputs and Outputs
- Dependencies
- Limitations
- Maintainer

Maintainer requires plain lines `Owner: actual owner` and `Contact: actual contact` or `Support: actual support path`. Do not prefix these fields with list markers or bold formatting. REPLACE_OWNER/REPLACE_CONTACT are rejected. Other invented or unfinished identities must not be submitted merely because they parse.

## Plugin

The input is a directory containing only `apc-plugin.json` and `skills/` at its root. The index must be a complete JSON document, not YAML. It requires integer schema_version 2, name/name_zh/name_en/version/description/description_zh and a nonempty skills array; unknown fields and duplicate keys are rejected. Copy and edit the [JSON template](../templates/plugin/apc-plugin.json).

Each declared path is exactly `skills/<canonical-name>`, without duplicates. Entries under skills/ must exactly match the declarations. Every embedded directory contains compliant v2 SKILL.md and skill-card.md files, and embedded frontmatter names are unique. Templates align directory and Skill names; update declarations and directories together when renaming.

Do not add a root README, packaging scripts or native `.codex-plugin` / `.claude-plugin` / `.hermes-plugin` manifests. APM derives native installation configuration; submit only the canonical payload.

## MCP

The input is one JSON file requiring schema_version, name, name_zh, name_en, version, description, description_zh and definition. schema_version is 2; duplicate keys and trailing JSON values are rejected. Use template fields only. The current decoder tolerates service-returned top-level type/message/content_md5 fields, but new resources do not need them; other unknown fields are rejected.

definition.transport is one of:

- stdio: nonempty string command; optional string-array args; url forbidden.
- http: absolute HTTPS url with a nonempty host and no userinfo; command and args forbidden.

Forbidden fields are rejected even when null, empty strings or empty arrays. Do not add unsupported env, headers or token fields. Supply the user's actual command or service address. Validation does not start MCP and cannot prove the executable exists, speaks the protocol or reaches a working service.

## Agent

The input is a resource root directory containing required `agent.yaml` and optional `assets/`. Do not submit a single YAML file, prebuilt tar.gz or URL. No extra root README/skill-card.md or enclosing version directory is allowed.

YAML is one document with a mapping root; all mapping keys are strings without duplicates. Require integer schema_version 2 and nonempty strings under agent: id/name/version/description/soul, plus the required display fields name_zh/name_en/description_zh. The former `zh_name` key was renamed to `name_zh`; `zh_name` is rejected rather than ignored. name is the Power identity and id is Bibit's internal identity, and both must hold the same value: APM publishes the resource to `<bibit root>/agents/<agent.name>/` (Bibit root defaults to `~/.bianbu-agent`, overridden by `BIANBU_AGENT_HOME`), while the Runtime addresses that directory by agent.id, so a mismatched pair installs and lists correctly but cannot be found by ID. APM does not compare them and does not treat a mismatch as a validation error; `apm validate --type agent` reports it as a warning only, so fix the resource itself and never rewrite an installed agents/ directory by hand.

The optional avatar key is `agent.metadata.avatar`: a canonical relative path to a regular `.png` file under `assets/`, 1 byte–1 MiB, square, with both dimensions 128–1024 pixels. Omit the field if no avatar is supplied; do not reference a nonexistent file. Other extension fields pass through unchanged; APM does not guarantee model/tools/permissions business semantics.

## Validation stages and error handling

1. `apm validate <path> --type <type> --json`: complete source-format validation, offline, without reading a Key. Check exit codes and stderr on failure; errors are not necessarily JSON.
2. `apm submit <path> --type <type> --message 'Describe the change'`: require a Key first, then repeat source validation and deterministically package/canonicalize. The final payload is at most 20 MiB; gzip source bytes divided by compressed bytes must not exceed 100. Only then upload. MCP uses JCS; the CLI creates tar.gz for other types. Do not manually generate upload archives or MD5 values.
3. Server acceptance, minimum validation, review and publication: query submission get. Local success does not imply remote success.

| Error clue | Repair |
| --- | --- |
| `schema_version 1 is not submittable` / `upgrade to 2` | Upgrade the resource to format v2: set `schema_version: 2` in every authoritative file and add `name_zh`/`description_zh` |
| `requires non-empty string name_zh` / `name_en` / `description_zh` | Ask the user for the real display names and description, then write them into the resource; never invent or machine-translate them |
| `name_zh is N characters, want 2-16` | Rewrite the Chinese display name to 2–16 characters; never pad it with spaces or punctuation |
| `name_en is N characters, exceeds 32` / `must be printable ASCII` / `must contain at least one ASCII letter` | Rewrite the English display name within 32 printable ASCII characters, keeping at least one letter |
| `description is N UTF-16 code units, exceeds 500` / `description_zh is N UTF-16 code units, exceeds 500` | Shorten the wording; the limit counts UTF-16 code units, not bytes, so a 500-character Chinese description is accepted while 251 emoji are not. APM enforces this offline because APC rejects the submission with a 400 that does not name the field |
| `agent.zh_name was renamed to agent.name_zh` | Rename the key; do not keep both |
| skill-card.md `requires integer schema_version 2` | Bump the card's frontmatter version to 2 (the card structure itself does not change) |
| requires non-empty string version / canonical | Fix strict version or name in the resource metadata |
| must start with YAML frontmatter | Remove BOM/leading whitespace and use a standalone opener |
| must start with "# Skill Card:" | Remove blank lines between closing frontmatter and title |
| section missing or empty / no non-empty Owner | Use exact headings and plain maintainer fields |
| placeholder | Obtain real maintainer information; do not invent contacts |
| undeclared / raw package | Match Plugin declarations to its closed directory set; keep only allowed Agent root entries |
| command/args/url presence | Remove fields belonging to the other transport, rather than emptying them |
| binary / archive / link | Identify the file; preserve user data and repair a submission copy according to its purpose |
| exceeds / decompression ratio | Remove irrelevant payload or reduce unnecessary content, then validate/submit again; do not pad garbage to alter the ratio |

Report the affected file, violated rule, modification and rerun result. If repair would change functionality, identify the information needed. Follow the CLI reference for review rejection and publication failures.

## Completion and upload approval

Format errors are repaired in the source; missing identity or display information is obtained from the user.

1. Repair only what the validator reports. Never change the user's capability, name, version or wording merely to
   make validation pass.
2. When a required field is missing or unusable — `name_zh`, `description_zh`, `Maintainer` owner/contact, a real
   MCP command or URL, the actual `soul` — stop and ask the user for that specific value. Do not invent a Chinese
   name, a translation, an email address or a service command. Report which field is missing, in which file, and why
   it cannot be completed without them.
3. Run `apm validate <path> --type <type> --json` after every repair until it passes. Validation success is not
   upload authorization.
4. Before uploading, present the user a short review summary and wait for explicit approval:

   ```text
   type: skill
   path: /path/to/my-skill
   name: my-skill
   version: v1.0.0
   name_zh: 我的技能
   name_en: Greeting Skill
   description_zh: 当用户请求一句简短问候时使用。
   message: Initial submission: greeting skill
   target: APC (APC_BASE_URL as configured) under the current APC_API_KEY identity
   ```

   State plainly that the upload publishes this content under the user's account, that a new version must be strictly
   higher than the previous published version, and that a published `(type, name, version)` can never be overwritten.
5. Run `apm submit` only after the user approves that summary. If the user does not approve, hand over the validated
   source directory and stop; do not submit, and do not ask for the API Key in chat.
6. Report the submission id and status after uploading; acceptance (`accepted` / `received`) is not publication
   (`published`).
