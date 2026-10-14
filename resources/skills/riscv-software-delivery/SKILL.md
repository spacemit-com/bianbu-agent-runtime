---
schema_version: 2
name: riscv-software-delivery
name_zh: RISC-V 软件交付
name_en: RISC-V Software Delivery
description: Plan RISC-V software installation, upgrades, and removal by coordinating Bibit MCP facts, local tools, Runtime approvals, and vetted package or artifact sources. Use when a requested package may be unavailable for riscv64.
description_zh: 协调 Bibit MCP 事实、本地工具、Runtime 审批与可信的软件包/制品来源，规划 RISC-V 平台上的软件安装、升级和卸载。当请求的软件包在 riscv64 上可能不可用时使用。
version: 1.0.0
---

# RISC-V Software Delivery

Use this Skill for package installation, upgrade, removal, or availability questions on
Bianbu `riscv64` devices, especially when the configured APT repository has no candidate.
The Skill coordinates the task; it never treats an LLM answer, a search result, or a URL as
execution authority.

## Roles and invariants

1. The cloud planner interprets the request and compares structured facts. It does not invent
   package URLs, select an unlisted source, grant approval, or run shell commands.
2. The Bibit MCP provides the package-domain tools. Use its read-only facts before proposing a
   write action. Its local 1.5B model is not responsible for open-ended source discovery.
3. Runtime owns task identity, candidate selection, approval, progress, cancellation, and the
   durable result. Approval must be bound to the selected candidate and immutable plan.
4. Local tools inspect the actual device and perform only the bounded step authorized by the
   approved plan. Re-read device state after every write step.

Never automatically add an APT repository, import a signing key, enable a foreign architecture,
run `curl | sh`, execute an installer script, download an arbitrary binary, or start a source
build. Do not use `sudo make install`. An x86_64 or aarch64 artifact is not a native riscv64
fallback.

## Workflow

### 1. Establish device facts

Collect the exact target, requested version and scope. Query the Bibit package MCP for:

- configured APT candidate and origin (`pkg_candidate`);
- installed version and command ownership when relevant (`pkg_verify`);
- simulated package impact (`pkg_operation_preview`) before any APT plan;
- package-name matching only through the read-only match-preview workflow.

Record `riscv64`, distribution release, enabled APT architectures, package-manager health,
installed state, available disk, and whether the request is system, user, project, or container
scope. A missing local APT candidate means only that the currently configured APT cache lacks a
candidate; it does not prove that the software is unavailable elsewhere.

### 2. Diagnose availability

Classify the outcome before suggesting a source:

- exact-name or package-type mismatch;
- configured repository contains a compatible riscv64 candidate;
- package exists but not for riscv64;
- repository configuration or cache freshness is unknown;
- upstream artifact exists but is not riscv64 or lacks required ABI/runtime compatibility;
- a vetted third-party artifact or vendor source may be available;
- only a source build is plausible;
- no viable native route is known.

Present these as facts. For unavailable x86/ARM-only applications, explain the native
incompatibility and offer verified alternatives only when a matching riscv64 candidate exists.
Do not silently recommend foreign architecture or emulation.

### 3. Discover candidates without execution

Use this source order for discovery, not automatic fallback:

1. Configured signed Bianbu or distribution APT source.
2. Curated, versioned Bianbu/SpacemiT vendor source.
3. Curated upstream or third-party release artifact with an exact riscv64 filename, version,
   SHA-256, source identity, and compatibility metadata.
4. Curated application runtime or sandbox package.
5. A pinned source-build recipe.

Treat Node.js unofficial builds as third-party artifacts, not as official Node.js releases. A
`linux-riscv64` archive may be shown only when the curated candidate record pins its exact
version, filename, SHA-256 from the matching release manifest, source identity, destination,
and uninstall method. It must not overwrite the system `node`, alter PATH globally, or install
as root by default. A candidate missing required trust or compatibility facts is information
only and cannot be selected for execution.

Assign every candidate one trust class:

- `configured_repo_signed`
- `vendor_curated_signed`
- `upstream_verified_release`
- `upstream_unverified`
- `community_repo_unverified`
- `source_build_verified`
- `source_build_unverified`

Only the first three, and a verified source build after its separate preflight, may advance to an
approval plan. Unverified candidates remain descriptive and require an explicit future policy;
they are not one-tap actions.

### 4. Select and freeze a plan

The user selects a displayed `candidate_id`; do not infer a selection from a vague reply such as
"continue" when multiple candidates exist. Runtime creates a `plan_id` containing:

- task and candidate IDs, normalized target, requested version, scope, and device facts;
- source identity, immutable URI or repository record, trust class, architecture, and ABI;
- artifact version, filename, digest and signing evidence, or source repository/tag/commit and
  build-recipe digest;
- download, unpack/build disk usage, dependencies, installation destination, and uninstall plan;
- package simulation result, verification conditions, owner, expiry, and plan revision.

Any change to the source, URI, package/artifact version, digest, architecture, dependencies,
installation destination, or build recipe invalidates approval and requires a new plan.

### 5. Handle source builds separately

Use a source build only after the candidate preflight reports a pinned revision, build and runtime
dependencies, toolchain availability, expected network access, free disk/memory, estimated cost,
output location, and an uninstall manifest. Build in a user-owned isolated workspace. Preserve
the build log, artifact checksums, installed-file manifest, and final verification result. Do not
retry a failed or interrupted build until current state has been inspected.

### 6. Execute and verify

Before execution, Runtime must show the exact selected source, trust level, changes, privilege
scope, size/cost, and recovery behavior, then obtain a confirmation bound to `plan_id`. During
execution, emit truthful stages such as waiting for approval, downloading, verifying, installing,
building, and verifying. Cancellation stops future safe steps; unknown results are reconciled
from device state, never replayed automatically.

Verify the expected package/artifact version, architecture, ownership, executable or desktop
entry where applicable, and the requested installation scope. Report success, partial success,
failure, or unknown result with the retained logs and a safe next step.

## Runtime gap handling

If Runtime does not yet expose candidate cards, immutable approval binding, progress/cancellation,
or the required Bibit MCP tool, stop after read-only diagnosis. Record the missing interface as
the RISC-V multi-source delivery joint-integration backlog item. Do not create a private bypass,
persist approval in prompt text, or replace the Runtime contract with a direct shell path.
