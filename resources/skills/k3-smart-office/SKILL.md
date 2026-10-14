---
schema_version: 2
name: k3-smart-office
name_zh: K3 智能办公
name_en: K3 Smart Office
description: Produce real office deliverables - weekly reports, meeting minutes, project plans, data reports and slide decks - by combining the built-in template catalog with the document app and the local file search. Use when the user wants a document, spreadsheet or deck file instead of an application window.
description_zh: 结合内置模板目录、文档应用与本地文件检索，产出周报、会议纪要、项目计划、数据报表和演示幻灯片等真实办公交付文件。当用户要的是文档、表格或演示文件而不是应用窗口时使用。
version: 1.0.0
---

# K3 Smart Office

Use this Skill when the deliverable is a **file**: a report, minutes, plan, data table or
deck. The execution itself belongs to the Bibit MCP tools (`template_*`, `appcli_run`,
`file_search`); this Skill decides what to produce, in which order, and what evidence the
answer must carry.

## 1. Pick a template before writing anything

| The user asks for | Template |
|---|---|
| 周报 / 日报 / 月报 / 工作汇报 / 进度同步 | `weekly_report` |
| 会议纪要 / 讨论记录 / 行动项 | `meeting_minutes` |
| 排期 / 项目计划 / 任务分解 / 里程碑 | `project_plan` |
| 数据整理 / 汇总表 / 统计报表 | `data_report` |
| PPT / 汇报幻灯 / 演示材料 | `presentation` |

1. `template_list` — see what exists and which document type each template produces.
2. `template_get` — get the skeleton, the minimum inputs (with fallbacks), the knowledge
   hints and the **ordered action plan**. `preset` selects the export format; only the
   formats the template declares are allowed.
3. Nothing fits? Fall back to `appcli_list` plus the `appcli-router` Skill; never invent an
   action name.

## 2. Draft first — do not interrogate the user

- The template's `inputs` carry a `fallback` for every field. Use it instead of asking.
- Ask **at most two** questions, and only when the answer changes the structure of the
  document (for example: which project the report is about).
- Safe defaults beat questions: produce the draft, then say what you assumed.
- Never invent facts to fill a section. Unknown stays unknown: write a short `待补充`
  (or `TODO`) line and mention it in the answer.

## 3. Ground the content

1. **Local first.** `file_search` finds candidate files on this device; it returns paths and
   metadata only, so **read the file before quoting it** with `file_read` (it only reads text
   files inside the filesearch index directories; docx/pdf come back as `file_binary`).
   Skip this when the request is self-contained ("write a project plan for X").
2. `kb_sources` shows which cloud knowledge sources the user enabled. Query the enabled
   remote ones with `kb_search` (one query; `source`/`top_k` optional) - for example a
   community knowledge base with remote vector search. `kb_search` **refuses to make a
   network call when nothing is enabled**, and it sends only the sanitized query text:
   never file contents, never a whole document.
3. Public/open-web facts go through Runtime's own `web_search`. The `web` entry in
   `kb_sources` is just a switch; `kb_search` will not run it (`kb_delegated`).
4. Treat `kb_disabled` / `kb_no_token` / `kb_auth_failed` / `kb_timeout` / `kb_bad_response`
   as **no external evidence**: report which source failed and why. Do not silently switch
   sources, do not retry in a loop, and do not replace the missing evidence with guesses.
5. Put the sources you actually used into the template's 资料来源 / Sources section: local
   file paths, or the `title` and `url` that `kb_search`/`web_search` returned. If there
   were none, say "无（基于对话内容）" rather than leaving it out. Never invent a citation.

## 4. Execute the plan

- Run the plan steps in order with `appcli_run` (`appcli=libreoffice`, `action` and `args`
  exactly as the plan gives them).
- `<project>` is the working project file, `<output>` is the finished file — both live in
  the appcli workspace, and the output extension must match the chosen preset.
- A write action asks the user for approval once. That is expected: do not pre-announce it,
  do not retry after a denial.
- Calc plans are per-cell: fill only the rows you need. The rows in the template are
  examples, not a quota.

## 5. Verify, then report

- Trust the run's artifact verification, or re-check with `appcli_verify`.
- Report: which template, the artifact path and size, the verification verdicts, how many
  actions ran, the sources used, and anything you left as `待补充`.
- On failure report the reason (missing prerequisite, approval denied, timeout, non-zero
  exit, malformed output, failed artifact check) and stop. Never "make it work" with a raw
  command line.

## Known limits (do not work around them)

- `writer_add_table` only creates an **empty** grid: the appcli has no command to fill a
  Writer table cell (`set-text` refuses a `table` item). If the content is tabular, use the
  Calc templates (`project_plan`, `data_report`) whose cells are writable, or write the rows
  as a list. Do not go read the appcli source looking for a cell command.
- `file_read` only reads **text** files inside the filesearch index directories
  (`~/Desktop`, `~/Documents` by default). A `.docx`/`.pdf`/`.xlsx` candidate comes back as
  `file_binary`: there is no text-extraction endpoint on this device today. Cite the file
  name/path and say the content could not be read - never summarise it from the name.
- `kb_search` sends the query text to the user's configured endpoint. If it returns
  `kb_disabled`, the user has not enabled any cloud source: do not try to work around it
  with a shell command or a different host.
- If an action is not in the template plan and not in `appcli_list`, it does not exist. Say
  so instead of exploring the installed package with the shell.
- The same is true for formats: only the presets listed in the template's `allowed` export
  formats are verified on this device.

## Boundaries

- `template_list` and `template_get` are read-only: they never run anything and never write
  a file. Producing the file is `appcli_run`'s job, and only a verified artifact counts — an
  exit code is not a result.
- Never run the appcli binary directly, never pass a free-form command line, never enter its
  REPL, and never install a prerequisite yourself (`pip install`, `apt install`, installer
  scripts): missing prerequisites go through `dep_plan` and then `dep_apply`.
- `escalate=needs_host_channel` means the action needs the host desktop and was not executed
  here — hand it over instead of retrying inside the sandbox.
- Opening an application window is a different path (the application-launch tools), not
  `appcli_run`.
