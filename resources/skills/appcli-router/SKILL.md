---
schema_version: 2
name: appcli-router
name_zh: AppCLI 产物路由
name_en: AppCLI Router
description: Produce deliverable files such as PDFs, documents, spreadsheets, diagrams and exported media by driving the registered CLI-Anything app CLIs through the appcli tools. Use when the deliverable is a file rather than an application window.
description_zh: 通过 appcli 工具驱动已注册的 CLI-Anything 应用，产出 PDF、文档、表格、图表和导出媒体等交付文件。当交付物是文件而不是应用窗口时使用。
version: 1.0.0
---

# AppCLI Artifact Router

Use this Skill when the deliverable is a **file** — a PDF, document, spreadsheet, diagram,
exported image or media clip — instead of an application window on the desktop. The actual
execution belongs to the Bibit MCP appcli tools: this Skill only decides which tool to call
and what evidence the answer must carry.

## Two paths that must not be confused

| The user asks for | Use | Not |
|---|---|---|
| A window to appear ("open the terminal", "open GIMP") | the application-launch path | `appcli_run` |
| A file to exist ("export this report to PDF", "make a PNG diagram") | `appcli_run` | the application-launch path |
| An existing `.drawio` file converted to PNG/SVG | the `drawio` Skill (real `drawio` CLI) | `appcli_run` unless it is a from-scratch diagram |

Opening an application never produces an artifact, and an appcli never opens a window. If the
request is ambiguous, ask which one the user wants instead of trying both.

## Workflow

1. **Discover before calling.** Call `appcli_list` to see the registered apps, their
   actions, their parameters, whether their prerequisites are satisfied, and their riscv64
   acceptance status. Never guess an app name, an action name or an argument key: an
   unregistered one is rejected, and guessing wastes a round trip.
2. **Prerequisites are not installed by you or by the appcli tools.** `appcli_run` refuses
   to execute when something is missing and tells you what is missing, which channel it needs
   (apt needs sudo, Python packages go into the user environment), and that the install path is
   a dependency plan. Build that plan (`dep_plan`), get approval, and only then apply it
   (`dep_apply`). Never run `pip install`, `apt install`, an installer script or a
   `git+https://` source directly.
3. **Run one action with registered arguments.** Call `appcli_run` with `appcli`, `action`
   and `key=value` arguments; paths are the `project` (source) and `output` (target) parameters
   when the action declares them. One call produces one artifact.
4. **The artifact is the result, not the command.** `appcli_run` verifies the produced file
   (existence, size, format magic, truncation markers) and fails when the file is missing or
   half-written, even if the command exited `0`. Report the artifact path, its size and the
   per-rule evidence. Use `appcli_verify` to re-check a file that was produced earlier, or to
   confirm a file after a failed run.

## Boundaries

- Never run an appcli binary yourself, never pass a free-form command line, and never enter its
  interactive mode: `appcli_run` accepts registered actions only and refuses a call with no
  subcommand precisely because that would enter a REPL.
- Never report success from an exit code, from the appcli's stdout, or from the plan you made.
  Only a verified artifact counts; with no artifact there is no result.
- Never bypass the riscv64 acceptance status. If `appcli_run` reports that an app is not
  verified on this device, say so — do not fall back to the raw binary to "make it work".
- `escalate=needs_host_channel` means the action needs the host desktop and was not executed in
  the sandbox. Hand the request to the host; do not retry it inside the sandbox.
- After a timeout or a cancellation, nothing is replayed automatically. Report the state of any
  partial file and let the user decide the next step.
- Read-only appcli actions (`appcli_list`, `appcli_verify`) never need approval; the
  artifact-producing actions do, so do not retry a denied call without new arguments.

## Answer contract

A finished answer states the appcli and action used, the artifact path and size, the per-rule
verification verdicts, and — on failure — the reported reason (missing prerequisite, channel,
timeout, non-zero exit, malformed output, or failed artifact check) together with what the user
can do next.
