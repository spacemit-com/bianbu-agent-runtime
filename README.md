# Bianbu Agent Runtime

English | [简体中文](README_cn.md)

Bianbu Agent Runtime is a YAML-configured agent service and the runtime core of
Bianbu AgentOS. It owns sessions, agent lifecycle, model calls, tool and plugin
execution, and the security boundary around them.

- **Sessions are event logs.** A session is one continuous `seq` log. Refreshing,
  switching sessions and watching from several clients all project the same
  history, and switching pages never stops a background run.
- **Pluggable.** Plugins contribute tools, skills, hooks, MCP servers and console
  pages.
- **Secure by construction.** Tool approval, per-agent capability sync, the
  securityd trust chain and fail-closed production mode are built in.
- **Models are configuration.** A provider stores connection details only; the
  model name lives in each agent's own `agent.yaml`.
- **Built for the edge.** The current release targets SpacemiT K3 (riscv64).

## Documentation

| Document | Contents |
| --- | --- |
| [HTTP API guide](docs/api-guide.md) | `/api/v1` routes, error codes, session and event-stream semantics |
| [CLI guide](docs/cli-guide.md) | `bianbu-agent` commands and resource management |
| [Plugin development guide](docs/plugin-development-guide.md) | Plugin layout, hook/tool identity and capability loading |
| [Architecture overview](docs/overview-guide.md) | Layering, process model and key design decisions |
| [Security plugin](docs/security-plugin.md) | How `bianbu.security` maps runtime hooks to securityd |
| [DeepSeek Harness notes](docs/deepseek-harness-notes.md) | Behaviour mirrored from DeepSeek Harness, with source evidence |

## Architecture

### Processes

```
bianbu-agent-runtime.service            (one per device, starts at boot)
└─ Control plane (FastAPI, /api/v1, WS /api/v1/events/*)
   ├─ Sessions, runs, events, files, SQLite stores
   ├─ Agent and resource inventory
   ├─ WorkerSupervisor ──► Worker per agent
   │                         ├─ models, tools, MCP, skills
   │                         ├─ PluginGeneration (including bianbu.security)
   │                         └─ security middleware (DLP / approval / deny)
   └─ SecurityRuntimeClient ──► securityd (prepare/commit/abort start, leases)
```

- **Control plane**: holds session, agent and resource state, serves HTTP and
  WebSocket APIs, and manages worker lifetimes.
- **Worker**: executes runs. Model calls, tools and hooks all happen here, so a
  run never blocks the control plane.
- **Execution mode**: an agent declares `host` or `sandbox`; securityd resolves
  the effective mode and issues the launch lease.
- **File-backed resources**: configuration, plugins, models and sessions are
  ordinary files under `data_dir`, readable and versionable without a database.

### Source layout

| Path | Responsibility |
| --- | --- |
| `bianbu_agent/api/` | `/api/v1` routers: agents, sessions, runs, security, packages, monitoring |
| `bianbu_agent/server/` | Composition root, runtime bootstrap, worker supervision |
| `bianbu_agent/sessions/` | Session log, projections, SQLite and JSON stores |
| `bianbu_agent/runs/` | Run manager, worker RPC, checkpoints |
| `bianbu_agent/engines/` | `AgentEngine` contract and the default LangGraph implementation |
| `bianbu_agent/tools/` | Built-in tools (files, processes, skills, web, browser, vision, kanban) |
| `bianbu_agent/plugins/` | Plugin host and contribution loading |
| `bianbu_agent/hooks/` | Hook protocol, command hooks, failure handling |
| `bianbu_agent/mcp/` | MCP server installation, health and credentials |
| `bianbu_agent/security/` | securityd client, middleware, sandbox, DLP, syscall policy |
| `bianbu_agent/console/web/` | Bundled web console assets |
| `bianbu_agent/cli/` | `bianbu-agent` command line |
| `resources/` | Starter agent, system plugins, models and MCP definitions shipped by the package |
| `debian/` | Debian packaging, systemd units, polkit and provisioning scripts |

### Sessions, runs and events

Session history uses a persistent event log plus client-side folding. JSON and
SQLite run stores write user messages, reasoning, text deltas, tools, approvals,
the session inbox and the run terminal state into one `seq` log per session.
`GET /api/v1/sessions/{id}/events` paginates history, `WS /api/v1/events/mux`
pushes committed events for every session, and `WS /api/v1/events/host` pushes
the session list and run state. Clients de-duplicate by `seq` and refetch the
authoritative history after a gap.

The console keeps one view model per session and projects the same event window
into Chat and Trajectory. Chat is for reading the conversation; Trajectory shows
user input, reasoning, assistant output, tools, approvals, context compaction and
terminal state per run, with search, raw input/output, durations and token
totals. Both views share history pagination and the live mux; there is no second
trajectory store.

## Quick start

> **Platform:** the current release only supports SpacemiT K3 (riscv64, Bianbu
> 4.0). Other platforms are untested, and the commands below plus
> `scripts/dev-provision.sh` assume that device.

### 1. Prepare the environment (once)

```bash
git clone <repository-url> bianbu-agent-runtime
cd bianbu-agent-runtime
./scripts/dev-provision.sh
```

`dev-provision.sh` is idempotent and only prepares the environment; it does not
start the runtime. It installs or upgrades dependencies from APT (matching
`debian/control`), installs `uv` when missing, adds the current user to the
`bianbu-agent` and `bianbu-security` groups, starts securityd and memoryd and
waits for their sockets, and prepares SearXNG and local model permissions.

The script needs `sudo`. For non-interactive use (CI, remote SSH) pass a password
through `SUDO_ASKPASS`. Group membership takes effect after a new login or a new
terminal.

### 2. Start it

```bash
./run.sh
```

`run.sh` only prepares the development configuration and then `exec`s the
source runtime: it exports development defaults, merges
`config/development.yaml` with the development self-signed security manifest into
`.bianbu-agent/config-all.yaml`, provisions the starter agent, registers the
development security identity (both need `sudo`), stops an installed service
holding the same port, and starts the runtime from the source tree.

Open <http://localhost:9899/chat> for the console.

### 3. Start manually

```bash
uv sync --locked
export BIANBU_SYSTEM_RESOURCE_ROOT="$PWD/resources"
uv run bianbu-agent --config config/development.yaml model bootstrap-chat
uv run --locked bianbu-agent --config config/development.yaml
```

`model bootstrap-chat` interactively asks for provider connection details and a
model name, then writes them to the local data directory. It exits immediately
when a configuration already exists. The runtime generates agent ids of the form
`com.bianbu.<12 hex characters>` and prints them; every agent can be deleted.

Source builds and SpacemiT wheels on Linux riscv64 both use Python 3.14. The
repository's `.python-version` makes `uv` select it, and `uv.lock` pins every
dependency.

## Configuration

Only provider connection details are stored globally. The model name is written
into each agent's `agent.yaml`:

```text
models/
├── openai/
│   └── provider.yaml
└── anthropic/
    └── provider.yaml

agents/writer/agent.yaml:
  model:
    provider: openai
    name: gpt-5
    effort: low
```

The runtime resolves exactly the `provider + name` stored on the agent; there is
no global default provider or model. `effort` is stored with that model
selection and accepts `off`, `low`, `high` or `max`; omitting it means `low`.
`provider.yaml` is mode `0600` because it may contain an API key.

In an agent configuration, `name` is the call-facing name, while `name_zh` /
`name_en` are display names for the console and Bibit in their respective
languages; `description` / `description_zh` are the matching UI summaries and
fall back to `name` when empty. `description` never enters the LLM context; model
behaviour is defined by `soul`. New `agent.yaml` files use `schema_version: 2`,
matching AgentHub's Power format v2; version 1 records remain readable. The agent
HTTP API uses the same fields and `soul`, and no longer accepts the old
`instructions` field.

### Data directory

`data_dir` can contain `agents/`, `models/`, `sessions/`, `skills/`, `mcps/`,
`plugins/`, `workspace/` and `logs/`. Extension roots such as `skills/`, `mcps/`
and `plugins/` are created on demand by the managers that write them. A
standalone MCP lives in `mcps/<mcp-id>/`; a bundled plugin lives in
`plugins/<plugin-id>/` and may contribute hooks, tools, skills, console pages and
several MCP servers under `mcps/<name>/`.

Service and resource commands take an explicit `--config`. Development commands
resolve `runtime.yaml` in the project, then `BIANBU_AGENT_CONFIG`:

```yaml
data_dir: .

service:
  title: Bianbu Agent
  host: 0.0.0.0
  port: 9899
```

A relative `data_dir` is resolved against the configuration file's directory and
`~` expands, so the configuration can live in `~/.bianbu-agent/config.yaml`, a
project directory or anywhere else. The runtime has no global default agent;
every session records the agent id it uses. HTTP access uses `/api/v1` only.

## Security boundary

### Tool approval

When a tool declares `requires_approval=true`, every execution waits for user
approval. The runtime also inspects `terminal` command arguments: a standalone
`rm` command word, including inside scripts, heredocs, wrappers and subshells,
forces approval before anything executes.

### Agent security lifecycle

After an agent is persisted (the default agent, API-created experts and imported
`.agent` packages), the runtime registers it with securityd and replaces its
capabilities, tools, skills, MCP servers and hooks before starting a worker. This
logical identity registration does not require a per-agent signed manifest. A
controlled worker lease can still be enabled separately in production. Deleting
an expert calls securityd's `UnregisterAgent` first and removes the local
declaration only after that succeeds; a failed unregister keeps the expert and
returns an error.

`./run.sh` is development-oriented: it sets `BIANBU_SECURITY=1`,
`BIANBU_RUNTIME_SECURITY=required`, `BIANBU_SANDBOX=off`, and applies no cgroup
`MemoryMax` / `CPUQuota` / `TasksMax` to workers, because a single-board device
has no other tenant to protect while a long session can be killed by a fixed
limit. Pass a value to reproduce limits, for example
`BIANBU_WORKER_MEMORY_MAX=2G ./run.sh`. Set `BIANBU_SANDBOX=agent` to exercise
the controlled sandbox lease.

Production must be launched by a trusted runtime manager with
`BIANBU_RUNTIME_MODE=production` and `BIANBU_RUNTIME_SECURITY=required` so it fails
closed; `./run.sh` refuses to start production mode directly.

Per-agent YOLO mode is available on the Security / capabilities page. It allows
every current, unknown and future capability or scope without confirmation, but
keeps caller validation, sandboxing, network policy, DLP, auditing and securityd
fail-closed behaviour. securityd stores the mode authoritatively; disabling YOLO
restores the original granular grants. Never emulate YOLO with environment
variables or agent configuration.

### Deployment notes

Plugins are trusted Python code inside the host process, not an OS sandbox. Do
not commit real credentials or runtime data. The current release disables the
admin token gate for the web and management APIs in both development and Debian
production mode, so the console needs no manual token; deploy it only on trusted
networks, and add authentication, TLS and access control at a reverse proxy or
gateway before exposing it publicly. stdio MCP servers also start child processes
as the service user, so install only trusted configurations and programs.

## Integrations

### securityd

The runtime talks to `bianbu-securityd` over D-Bus `io.bianbu.Security` and the
securityd UDS. The control plane prepares, commits and aborts worker starts,
receives the effective execution mode and lease, and passes the lease identity to
the worker. Workers run the `bianbu.security` plugin, which maps runtime hooks to
securityd hook points:

| Runtime hook | securityd hook point |
| --- | --- |
| `BeforeRun` | `agent_startup` |
| `AfterContext` | `input` |
| `BeforeModel` | `pre_model_call` |
| `AfterModel` | `post_model_call` |
| `PreToolUse` | `pre_tool_call` |
| `PostToolUse` | `post_tool_call` |
| `AfterRun` | `output` / `agent_shutdown` |

See [Security plugin](docs/security-plugin.md) for the full mapping, DLP
behaviour and failure semantics.

### hermes-agent

The runtime registers a hermes agent with securityd and infers its framework
from the `framework` field or from a `com.hermes*` agent id, so the console shows
the hermes tool catalog for it. Runtime reconciliation never removes
`com.hermes.*` rows: securityd is the shared registry for runtime, hermes and
DeepSeek Harness agents.

The hermes-side adapter lives in the `bianbu-agent-security` repository
(`impl/bianbu-securityd/hermes_plugin/`). It is a hermes plugin that calls
securityd before tool execution, binds the profile-derived agent id
(`com.hermes.<profile>`) and applies DLP to tool arguments, tool results and the
final output. Install it with
`hermes_plugin/scripts/install-on-k3.sh hermes`. The runtime does not run hermes
itself and does not import hermes code.

### DeepSeek Harness (dsh)

DeepSeek Harness is a separate runtime. The runtime's security registration
already accepts `harness` agents, and securityd owns a dedicated `harness` tool
catalog, so a dsh agent appears in the same security console as
`com.dsh.agent`.

The dsh adapter lives in the `bianbu-agent-security` repository under
`impl/bianbu-securityd/harness_plugin/`: an out-of-tree Node bundle plus a Python
sidecar that speaks JSON-RPC over stdio and calls securityd. `tools/pre-execute`
maps to `pre_tool_call`, `tools/post-execute` to `post_tool_call` and
`agent/pre-step` to `input`. dsh exposes no public hook that can rewrite committed
assistant messages or provider requests, so the adapter deliberately does not
claim output-side masking. Install it from the security checkout with
`impl/bianbu-securityd/harness_plugin/scripts/install-on-k3.sh dsh`.

DeepSeek Harness is also a behavioural reference for this repository. Design
notes with upstream revisions and file-level evidence live in
[DeepSeek Harness notes](docs/deepseek-harness-notes.md).

## Models, tools and MCP

Each agent stores its model in `agent.yaml` and resolves it at run time. The
default engine is LangGraph, but `AgentEngine` is an abstract contract and
`engine_builder` can inject another implementation; plugin contracts never
mention LangGraph.

Built-in tool sources live in `bianbu_agent/tools/`. User tool directories are
for extensions only. MCP servers are installed from the console MCP page or with
the CLI. A stdio MCP command is parsed according to POSIX argument rules and
executed directly, without a shell, so pipes, redirection and command
substitution are not supported. Install only trusted sources and grant the
smallest directory you need.

```bash
bianbu-agent --config ~/.bianbu-agent/config.yaml \
  mcp add filesystem --command npx \
  --args -y @modelcontextprotocol/server-filesystem /data/projects
bianbu-agent --config ~/.bianbu-agent/config.yaml mcp list
bianbu-agent --config ~/.bianbu-agent/config.yaml mcp remove filesystem
```

Installing or removing an MCP server requires restarting the running runtime so
the new server enters the active generation.

## K3 and Bibit Intent

On K3, on-device intent and model capabilities come from the separate
`bibit-intent` Debian package, which `bianbu-agent-runtime` depends on. Refreshing
the index and installing or upgrading the runtime pulls in the matching Bibit
package.

Bibit owns the lifetime of its on-device models and services. It installs and
globally enables `bibit-intent.service`, whose `Wants` pulls in
`bibit-intent-models.service` to prepare `bibit-llm` / `bibit-embed` in the AI
gateway. The runtime (including `./run.sh`) never enables or starts them; it
probes `http://127.0.0.1:8765/healthz` once and reports readiness without blocking
startup, so conversations fall back to the cloud when the edge model is
unavailable.

```bash
systemctl --user is-active bibit-intent bibit-intent-models
curl -fsS http://127.0.0.1:8765/healthz
curl -fsS http://127.0.0.1:9899/api/v1/runtime/status
```

Production workers call Bibit over the stable loopback HTTP endpoint, so
`/opt/bibit-intent` is not mounted into the sandbox. The signed network policy
allows `localhost` for that loopback service only, not the LAN or arbitrary
internet hosts.

## Debian package

The package installs one system-level service,
`bianbu-agent-runtime.service`, wanted by `multi-user.target`: it starts at boot
regardless of who logs in. Its identity is the device user resolved at install
time, recorded in `/etc/bianbu-agent-runtime/service-user` and published through
the drop-in `/etc/systemd/system/bianbu-agent-runtime.service.d/10-service-user.conf`
(because `User=` cannot read a configuration file). Running as the device user
is deliberate: host workers keep the real login identity, and home directories,
file resources, sudo and PAM depend on it. If no device user can be found (image
chroot, container, CI), the packaged `bianbu-runtime` account is kept and the
unit is not enabled.

Persistent state lives in the service user's `~/.bianbu-agent/`. The console port
is derived from that account's UID: 9899 for UID 1000, otherwise
`9899 + ((uid - 1000) % 55637)`. Host and bwrap workers use system transient
scopes, and polkit only admits calls originating from
`bianbu-agent-runtime.service`, so a local session gets no unit management
rights. Open the console in a browser at that port.

The system package must be installed once; the service starts at boot:

```bash
sudo systemctl status bianbu-agent-runtime.service
journalctl -u bianbu-agent-runtime.service
sudo systemctl restart bianbu-agent-runtime.service
```

There is exactly one data directory per device, so callers resolve it in this
order:

1. `BIANBU_AGENT_CONFIG` (explicit, highest priority);
2. the account recorded in `/etc/bianbu-agent-runtime/service-user`, using its
   `~/.bianbu-agent/config.yaml`; the `bianbu-agent` CLI and workers both follow
   this, so `sudo bianbu-agent model list` never reads `/root/.bianbu-agent`;
3. the caller's own `~/.bianbu-agent/config.yaml` when no install record exists
   (source checkout, development machine).

`/api/v1/runtime/status` also reports `service_user` and `data_dir`, and the
console's Runtime status page shows both. Development can still override the
configuration with `BIANBU_AGENT_CONFIG=/path/config.yaml`.

Preinstalled agents, skills, plugins, MCP servers and model definitions are
shipped by the system package under `/usr/share/bianbu-agent-runtime/` and are
replaced on upgrade. Built-in tools live in the Python package's
`bianbu_agent/tools/`; the default agent's stable instance lives in
`/var/lib/bianbu-agent-profiles/`. AgentHub installs and edited resources are
written to `~/.bianbu-agent/`, and a user definition with the same id takes
precedence. The deployment language setting `deployment.language` supports only
`zh` (Simplified Chinese) and `en` (English).

## Build from source

```bash
uv sync --locked                 # create the locked Python environment
uv build                         # build the wheel/sdist
dpkg-buildpackage -us -uc -b     # build the Debian package
```

The Debian build creates a relocatable, locked virtualenv under
`/usr/lib/bianbu-agent-runtime` and runs the packaging tests in
`debian/tests/`. On riscv64 it downloads the
pinned SpacemiT `sqlite-vec` wheel and verifies its SHA-256.

Install the resulting `bianbu-agent-runtime_<version>_<arch>.deb` with
`sudo apt install ./bianbu-agent-runtime_*.deb`.

## Verification

```bash
uv run pytest -q
uv build
```

Development records, release notes and on-device acceptance results live in
`debian/changelog` and the documents above.
