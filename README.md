# 🤖 Code Review Agent

A self-hosted, multi-agent code review system for **GitLab Merge Requests**, powered by **Ollama + DeepSeek** (or any local LLM).

---

## Architecture

```
GitLab MR Opened/Updated
        │
        ▼
  Webhook → FastAPI Server
        │
        ▼
  Shallow clone of MR source branch (Source Code)
        │
        ▼
  Language Detector  ──────────────────────────────────────┐
        │                                                  │
        ▼                                                  │
  Roslyn (C#) · ESLint (JS/TS) · Ruff (Python)              │
  Clippy (Rust) · Clang-Tidy (C/C++) · SpotBugs (Java)      │
        │                                                  │
        ▼                                                  │
  Static Analysis Results                                   │
        │                                                  │
        ▼                                                  │
  LLM Prompt (diff + static findings) ◄─────────────────────┘
        │
        ▼
  CoordinatorAgent
  ├── CodeQualityAgent   → bugs, logic errors, smells
  ├── SecurityAgent      → OWASP Top 10, secrets, injection
  ├── PerformanceAgent   → N+1 queries, blocking I/O, caching
  └── DocumentationAgent → missing docstrings, outdated comments
        │
        ▼
  Ollama (local LLM) → AI Review
        │
        ▼
  Merge Results (static findings + AI review) → Deduplicate + Prioritise
        │
        ▼
  LLM Summary Generation
        │
        ▼
  Post inline + summary comments → GitLab MR
```

Static analysis runs per changed file based on its detected language, and its
findings are (a) merged directly into the final results and (b) injected into
each specialist agent's LLM prompt so the model doesn't re-report what a linter
already caught. If the MR source branch can't be cloned (no `git`, network
issue, etc.) the pipeline degrades gracefully to LLM-only review — nothing
fails, that stage is just skipped. See [Static Analysis Tools](#static-analysis-tools) below.

**Diff-scoped, not whole-file.** Linters run against each changed file's
current full content on disk (they need real, complete source to parse), but
the coordinator then drops any finding that isn't on a line the diff actually
added — so a pre-existing issue elsewhere in a touched file won't show up as
if this MR introduced it. File-level findings (no specific line) are always
kept. This matches how the LLM agents already behave, since they only ever see
the diff chunk, never the whole file.

**Ready-to-apply fixes, not just advice.** Each finding can carry a concrete
code fix — a line range plus the replacement source — in addition to the prose
recommendation. On GitLab that fix is posted as a native
[suggestion block](https://docs.gitlab.com/ee/user/project/merge_requests/reviews/suggestions.html)
inside the inline comment, so the MR author gets an **Apply suggestion** button
that commits the change in one click; no copy-pasting from a comment. To keep
that button safe to press, a fix is only kept when it is small (≤ 40 lines),
covers the reported line, and stays within the lines the model was actually
shown in the diff — anything else is dropped and the finding falls back to
the prose suggestion. The diff is sent to the model with explicit new-file
line numbers so the range is anchored exactly. `POST_FIX_SUGGESTIONS=false`
renders fixes as read-only code blocks instead; the local CLI prints them
under each finding.

---

## Prerequisites

| Tool   | Version  | Purpose                      |
|--------|----------|------------------------------|
| Python | ≥ 3.12   | Runtime                      |
| Ollama | latest   | Local LLM runner             |
| Docker | optional | Containerised deployment     |
| git    | any      | Clones the MR branch for static analysis |

---

## Static Analysis Tools

Each is optional and independent — install only the ones matching languages you
review. A missing tool is logged and skipped; the LLM-only review still runs.

| Language        | Tool     | Install                                              | Config (`.env`) |
|-----------------|----------|-------------------------------------------------------|-----------------|
| C#              | Roslyn (via `dotnet format`) | Ships with the [.NET SDK](https://dotnet.microsoft.com/download) | `DOTNET_CMD` |
| JS / TS         | ESLint   | `npm install -g eslint` (or a local `eslintrc` + `node_modules/.bin/eslint`) | `ESLINT_CMD` |
| Python          | Ruff     | `pip install ruff`                                     | `RUFF_CMD` |
| Rust            | Clippy   | `rustup component add clippy`                          | `CARGO_CMD` |
| C / C++         | Clang-Tidy | `apt install clang-tidy` / `brew install llvm`       | `CLANG_TIDY_CMD`, `CLANG_TIDY_ARGS` |
| Java            | SpotBugs | `apt install spotbugs` or download from [spotbugs.github.io](https://spotbugs.github.io/) | `SPOTBUGS_CMD`, `SPOTBUGS_CLASSES_DIRS` |

Set `ENABLE_STATIC_ANALYSIS=false` to disable the whole stage, or
`CLONE_FOR_STATIC_ANALYSIS=false` to keep it LLM-diff-only without ever cloning
the repo (useful if the server can't reach your GitLab host over git).

Adding a new language is a plugin, not a review-engine change: subclass
`StaticAnalyzer` in `app/static_analysis/`, decorate the class with
`@register_analyzer(Language.whatever)`, and import the module from
`app/static_analysis/__init__.py`. `StaticAnalysisRunner` reads the registry
(`app/static_analysis/registry.py`) rather than knowing about any specific
tool, so nothing else needs to change.

### Clang-Tidy notes

Clang-Tidy needs to resolve `#include`s to do anything beyond the most
trivial syntax checks, which normally means a compilation database
(`compile_commands.json`, produced by CMake/Bazel/`bear`/`compiledb`). If one
exists anywhere under the checkout, it's picked up automatically (`-p
<dir>`); otherwise `CLANG_TIDY_ARGS` (default `-- -std=c++17`) is passed as a
fallback so at least syntax-only checks still run. Point `CLANG_TIDY_ARGS` at
your own flags if the fallback misses too much for your codebase.

### SpotBugs notes

Unlike the other analyzers, SpotBugs analyzes compiled **bytecode**, not
source — the project must already be built (Maven's `target/classes`,
Gradle's `build/classes/java/main`, or another directory listed in
`SPOTBUGS_CLASSES_DIRS`). This analyzer does not compile the project itself
(a real Java classpath depends on external dependencies with no visibility
from here), so if none of the configured directories exist under the
checkout, it reports itself unavailable — `mvn compile` / `gradle
compileJava` before running a review that touches Java files.

### C# / Roslyn notes

Roslyn/MSBuild need a project to resolve anything, so this analyzer prefers
the nearest real `.sln`/`.slnx`/`.csproj` under the checkout. If the checkout
only has the changed file(s) — e.g. static analysis fell back to fetching
just the changed files via the GitLab API instead of a full `git clone` — it
synthesizes a minimal throwaway project around just those files instead of
reporting itself unavailable. That throwaway project only sees the changed
files, so it won't catch cross-file issues a real project/full clone would;
install `git` on the server for the more accurate path.

The synthesized project's target framework is auto-detected from `dotnet
--version` (e.g. dotnet 10 installed → targets `net10.0`), so no manual
configuration is needed when you install a different SDK version.

Also note: `dotnet format analyzers` silently reports zero findings when
pointed directly at a bare `.csproj` with no solution — confirmed against a
real dotnet 10 SDK, it logs `Skipping referenced project '<name>'` even
though `dotnet build` reports the exact same warnings fine. Every `.csproj`
this analyzer touches, real or synthesized, is therefore wrapped in a
throwaway solution file before running `dotnet format`.

---

## Execution Mode (CPU / GPU)

On startup, the server detects the CPU core count, total RAM, whether an
NVIDIA GPU is present (via `nvidia-smi`), and whether Ollama is actually
using it for the configured model (via `/api/ps`) — all logged as a single
`hardware.detected` line. `EXECUTION_MODE` then resolves to a concrete
concurrency policy:

| Mode   | Behaviour |
|--------|-----------|
| `auto` (default) | Picks `gpu` if a GPU was detected, else `cpu` |
| `cpu`  | Forces everything below to run **one at a time** — MR reviews, static analyzers, LLM requests — regardless of the `MAX_PARALLEL_*` values. A CPU-only box can't truly parallelise any of these, so this is a hard floor, not a default you can override upward by mistake. |
| `gpu`  | Uses `MAX_PARALLEL_REVIEWS` / `MAX_PARALLEL_ANALYZERS` / `MAX_PARALLEL_LLM_REQUESTS` as configured, gated with `asyncio.Semaphore` at each layer (concurrent MR reviews, concurrent static-analysis language groups, concurrent LLM requests). |

The resolved policy is logged once at startup as `execution.mode_resolved`.
If you're on a GPU box but reviews still run one at a time, check that log
line — `auto` only picks `gpu` if `nvidia-smi` actually reports a GPU;
force it explicitly with `EXECUTION_MODE=gpu` if detection is wrong for your
setup (e.g. a non-NVIDIA GPU).

---

## Quick Start

### One-command setup

Setup/run/service scripts live under [`setup/`](setup/), one folder per OS —
each folder is self-contained, so you only ever need the one matching your
machine:

```
setup/
├── windows/   setup.ps1, run.ps1, install-service.ps1, uninstall-service.ps1
├── linux/     setup.sh, run.sh, install-service.sh, uninstall-service.sh, code-review-agent.service
└── macos/     setup.sh, run.sh
```

Each `setup.*` checks for git/Python 3.12/Ollama, installs whichever are
missing via the OS package manager (`apt`/`brew`/`winget`), creates the
venv, installs `requirements.txt`, and bootstraps `.env` from
`.env.example`. Once `.env` exists, it reads `LLM_PROVIDER` from it and only
installs/pulls Ollama when that's actually the configured provider.

```bash
# Ubuntu / Debian
git clone https://github.com/your-org/code-review-agent.git
cd code-review-agent
./setup/linux/setup.sh
```

```bash
# macOS
git clone https://github.com/your-org/code-review-agent.git
cd code-review-agent
./setup/macos/setup.sh
```

```powershell
# Windows (PowerShell)
git clone https://github.com/your-org/code-review-agent.git
cd code-review-agent
.\setup\windows\setup.ps1
```

Useful flags: `--no-system`/`-NoSystem` (skip installing git/Python/Ollama —
just venv + pip + `.env`), `--no-ollama`/`-NoOllama`, `--model`/`-Model
<name>` (Ollama model to pull), `--static-langs`/`-StaticLangs
python,js,dotnet,rust,cpp,java` (also install the matching linter from the
[Static Analysis Tools](#static-analysis-tools) table).

After setup finishes, edit `.env` — at minimum set `GITLAB_INSTANCES` (see
[Multiple GitLab Servers](#multiple-gitlab-servers)) and whichever provider
key `LLM_PROVIDER` needs — then start the server:

```bash
./setup/linux/run.sh    # Ubuntu / Debian
./setup/macos/run.sh    # macOS
```

```powershell
.\setup\windows\run.ps1   # Windows
```

Server starts at http://localhost:8000.

### Manual setup

If you'd rather do it by hand instead of running the `setup/<os>/setup.*` script:

```bash
# 1. Install Ollama & pull a model (skip if using a cloud LLM_PROVIDER)
curl -fsSL https://ollama.com/install.sh | sh   # macOS/Linux; on macOS `brew install ollama` also works
ollama pull deepseek-coder-v2      # recommended — 16GB RAM
ollama pull deepseek-r1:7b         # lighter — 8GB RAM
ollama pull codellama:13b          # alternative
ollama pull qwen3-coder:latest     # newest Qwen coder release, strong agentic coding

# 2. Clone & configure
git clone https://github.com/your-org/code-review-agent.git
cd code-review-agent
cp .env.example .env
# Edit .env — set GITLAB_INSTANCES (see "Multiple GitLab Servers" below)

# 3. Install Python dependencies
python -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\Activate.ps1
pip install -r requirements.txt

# 4. Run the server
python main.py
# Server starts at http://localhost:8000
```

### 5. Configure GitLab Webhook

1. Go to your GitLab project → **Settings → Webhooks**
2. Set URL: `https://your-server.com/webhook/gitlab/<instance>` — `<instance>` is
   the key you gave this server in `GITLAB_INSTANCES` (e.g. `gitlab_com`)
3. Set Secret Token: that instance's `webhook_secret` from `.env`
4. Enable trigger: ✅ **Merge request events**
5. Click **Add webhook**

---

## Multiple GitLab Servers

The agent can review MRs across more than one GitLab server (e.g. `gitlab.com`
plus a self-hosted instance) — each one needs its own token, since tokens
aren't portable between servers. Configure them as a single JSON object in
`.env`, keyed by a short name you choose for each server:

```bash
GITLAB_INSTANCES={"gitlab_com":{"url":"https://gitlab.com","token":"glpat-xxxx","webhook_secret":"secret1"},"internal":{"url":"https://gitlab.mycompany.internal","token":"glpat-yyyy","webhook_secret":"secret2"}}
```

Then, in each GitLab server's **Settings → Webhooks**, point the webhook URL
at that server's own key: `https://your-server.com/webhook/gitlab/gitlab_com`
for the first, `https://your-server.com/webhook/gitlab/internal` for the
second. The agent looks up which URL/token/secret to use from the key in the
path — GitLab itself has no notion of your instance names, so this is how it
tells them apart.

For the manual trigger endpoint, pass the instance explicitly:

```bash
curl -X POST http://localhost:8000/review \
  -H "Content-Type: application/json" \
  -d '{"instance": "internal", "project_id": "myorg/myrepo", "mr_iid": 42}'
```

(`instance` can be omitted only when exactly one server is configured.)

Progress and report endpoints are namespaced by instance too:
`GET /review/status/{instance}/{project_id}/{mr_iid}`.

---

## Run as a systemd Service (Linux)

For a persistent deployment that survives SSH disconnects, crashes, and
reboots, instead of running `./setup/linux/run.sh` in a terminal:

```bash
# 1. Run it once by hand first — creates the venv and confirms it starts.
./setup/linux/run.sh
# Ctrl+C once you see "Application startup complete."

# 2. Install + start the service (fills in User/WorkingDirectory automatically
#    from this machine — no manual editing of code-review-agent.service needed)
sudo ./setup/linux/install-service.sh

# 3. Check status / logs
sudo systemctl status code-review-agent
journalctl -u code-review-agent -f
```

`code-review-agent.service` is a template (`__SERVICE_USER__`/`__WORKDIR__`
placeholders) — `install-service.sh` fills those in from the invoking user
and script location, writes the result to `/etc/systemd/system/`, then runs
`daemon-reload` and `enable --now`.

The unit runs `main.py` directly (not `run.sh`) so it doesn't re-run `pip
install` on every restart, and it runs with `UVICORN_RELOAD` unset —
`run.sh` enables uvicorn's auto-reload for interactive dev use, but a
service shouldn't re-exec itself on every file touch, so the unit stays in
stable no-reload mode. It's configured to start on boot and to restart
automatically 60 seconds after it stops for any reason other than an
explicit `systemctl stop` (`Restart=always`, `RestartSec=60`,
`StartLimitIntervalSec=0` — never gives up retrying). To pick up a code
change, `sudo systemctl restart code-review-agent`.

**Uninstall:**

```bash
sudo ./setup/linux/uninstall-service.sh
```

Stops and disables the service and removes the unit file from
`/etc/systemd/system/` — leaves the venv, `.env`, and the rest of the repo
untouched.

---

## Run as a Windows Service

For a persistent deployment that survives logout, crashes, and reboots,
instead of leaving `.\setup\windows\run.ps1` running in a terminal. Uses
[NSSM](https://nssm.cc/) (installed automatically via `winget` if missing)
to wrap `venv\Scripts\python.exe main.py` as a real Windows service — the
native equivalent of the systemd unit above.

```powershell
# 1. Run it once by hand first — creates the venv and confirms it starts.
.\setup\windows\run.ps1
# Ctrl+C once you see "Application startup complete."

# 2. Install + start the service (elevated/Administrator PowerShell)
.\setup\windows\install-service.ps1

# 3. Check status / logs
Get-Service code-review-agent
Get-Content logs\service.out.log -Tail 50 -Wait
```

Like the systemd unit, this runs `main.py` directly (no reload — reload
doesn't work on Windows for this app anyway, see `run.ps1`'s comment on
uvicorn forcing `WindowsSelectorEventLoopPolicy`, which breaks the
`nvidia-smi`/static-analysis subprocess calls made on every run). It's
configured to start on boot and restart automatically 60 seconds after any
non-explicit stop. To pick up a code change: `Restart-Service
code-review-agent`. Re-running `.\setup\windows\install-service.ps1` is
safe — it stops/removes any existing service of the same name first.

**Uninstall:**

```powershell
.\setup\windows\uninstall-service.ps1
```

Stops and removes the service — leaves the venv, `.env`, and the rest of the
repo untouched.

---

## Docker Deployment

```bash
# Start everything (Ollama + Agent)
docker compose up -d

# Pull the model inside the Ollama container
docker exec ollama ollama pull deepseek-coder-v2

# Check health
curl http://localhost:8000/health
```

---

## Local Testing (no GitLab needed)

```bash
# Review your last commit
python scripts/review_local.py

# Review a specific diff file
python scripts/review_local.py --diff my_changes.diff

# Pipe from git
git diff main...feature/my-branch | python scripts/review_local.py --stdin

# JSON output
python scripts/review_local.py --json
```

---

## Manual Trigger via API

```bash
curl -X POST http://localhost:8000/review \
  -H "Content-Type: application/json" \
  -d '{"instance": "gitlab_com", "project_id": "myorg/myrepo", "mr_iid": 42}'
```

`instance` matches a key in `GITLAB_INSTANCES` — see
[Multiple GitLab Servers](#multiple-gitlab-servers). It can be omitted only
when exactly one instance is configured.

---

## Dashboard

Open `http://<host>:8000/dashboard` for a live view of everything the agent has
reviewed. It is a single self-contained page (no CDN or external calls, so it
works on an air-gapped server) fed by `/api/reports` and `/review/active`.

- **Filters in one row** — date range (24h / 7d / 30d / 90d / all), GitLab
  instance, project, status and free-text search. Every figure, chart and table
  below re-computes against the same slice, and the filters live in the URL so
  a view can be bookmarked or shared.
- **Key figures** — reviews, issues found (with severity breakdown), fixes ready
  to apply, median review time, LLM request success rate and failed reviews,
  each with a change versus the previous period of the same length.
- **Charts** — issues over time by severity, reviews over time (failures
  highlighted), median review time trend, findings by agent, LLM time per
  request by agent, and hotspot files. Hover any mark for exact values; every
  chart has a **Table** toggle with the same numbers for screen readers,
  copy-paste, or when colour alone isn't enough.
- **Reviews table** — sortable, with in-progress reviews pinned on top showing
  agent progress. MR numbers link straight to GitLab.
- **Review detail** — click a row for the verdict, where the time went
  (static analysis / LLM / merge), severity mix, and every finding with its
  description, suggestion and ready-to-apply fix. Filter findings by severity
  or agent; expand *Execution details* for the per-request LLM table and
  analyzer runs. Deep-linkable via `#report=<file>`.
- **Themes** — follows the OS light/dark setting; the ◐ button pins a theme per
  browser, and `?theme=light|dark` forces one (handy for screenshots).

Reports are the JSON files `app/reporting.py` writes under `reports/` after
every review (webhook-triggered or local CLI), so the dashboard needs no
database — delete a file and it disappears from the dashboard.

---

## Configuration Reference (`.env`)

| Variable                  | Default                     | Description                              |
|---------------------------|-----------------------------|------------------------------------------|
| `OLLAMA_BASE_URL`         | `http://localhost:11434`    | Ollama API URL                           |
| `OLLAMA_MODEL`            | `deepseek-coder-v2`         | Model name (must be pulled first)        |
| `OLLAMA_TIMEOUT`          | `300`                       | Seconds before an LLM call times out — raise this if you see `ReadTimeout`/`RetryError` in the logs, larger local coder models can easily take minutes per request |
| `EXECUTION_MODE`          | `auto`                      | `auto` \| `cpu` \| `gpu` — see [Execution Mode](#execution-mode-cpu--gpu) above |
| `MAX_PARALLEL_REVIEWS`    | `1`                         | Concurrent MR reviews end-to-end (gpu/cloud mode only — forced to 1 in cpu mode) |
| `MAX_PARALLEL_ANALYZERS`  | `1`                         | Concurrent static-analysis language groups (gpu/cloud mode only — forced to 1 in cpu mode) |
| `MAX_PARALLEL_LLM_REQUESTS` | `1`                       | Concurrent LLM requests, across agents and MRs (gpu/cloud mode only — forced to 1 in cpu mode) |
| `GITLAB_INSTANCES`        | `{}`                        | JSON object of `{"name": {"url", "token", "webhook_secret"}}` — one entry per GitLab server (**required**, at least one). See [Multiple GitLab Servers](#multiple-gitlab-servers) |
| `MAX_DIFF_LINES`          | `2000`                      | Truncate diffs larger than this          |
| `MIN_SEVERITY_TO_COMMENT` | `low`                       | Only post issues at or above this level  |
| `POST_INLINE_COMMENTS`    | `true`                      | Post per-line diff comments              |
| `POST_SUMMARY_COMMENT`    | `true`                      | Post a top-level summary comment         |
| `POST_FIX_SUGGESTIONS`    | `true`                      | Post code fixes as GitLab *Apply suggestion* blocks (one-click commit); `false` shows them as read-only code |
| `ENABLE_CODE_QUALITY`     | `true`                      | Toggle Code Quality agent                |
| `ENABLE_SECURITY`         | `true`                      | Toggle Security agent                    |
| `ENABLE_PERFORMANCE`      | `true`                      | Toggle Performance agent                 |
| `ENABLE_DOCUMENTATION`    | `true`                      | Toggle Documentation agent               |
| `ENABLE_STATIC_ANALYSIS`  | `true`                      | Toggle the static analysis stage         |
| `CLONE_FOR_STATIC_ANALYSIS` | `true`                   | Shallow-clone the MR branch so analyzers have real files |
| `STATIC_ANALYSIS_TIMEOUT` | `60`                        | Seconds before a static analyzer subprocess is killed |
| `DOTNET_CMD`              | `dotnet`                    | Roslyn (C#) — binary name or full path   |
| `ESLINT_CMD`              | `eslint`                    | ESLint (JS/TS) — binary name or full path|
| `RUFF_CMD`                | `ruff`                      | Ruff (Python) — binary name or full path |
| `CARGO_CMD`               | `cargo`                     | Clippy (Rust) — binary name or full path |
| `CLANG_TIDY_CMD`          | `clang-tidy`                | Clang-Tidy (C/C++) — binary name or full path |
| `CLANG_TIDY_ARGS`         | `-- -std=c++17`              | Fallback compiler flags used only when no `compile_commands.json` is found under the checkout |
| `SPOTBUGS_CMD`            | `spotbugs`                  | SpotBugs (Java) — binary name or full path |
| `SPOTBUGS_CLASSES_DIRS`   | `target/classes,build/classes/java/main` | Comma-separated candidate build-output directories, checked in order; first one that exists is used |

---

## Running Tests

```bash
pytest tests/ -v
```

---

## Choosing a Model

| Model                  | RAM needed | Best for                        |
|------------------------|------------|---------------------------------|
| `deepseek-coder-v2`    | 16 GB      | Best code understanding ✅      |
| `deepseek-r1:7b`       | 8 GB       | Lightweight, good reasoning     |
| `deepseek-r1:14b`      | 16 GB      | Better accuracy                 |
| `codellama:13b`        | 16 GB      | Alternative open-source option  |
| `qwen2.5-coder:14b`    | 16 GB      | Strong competitor to DeepSeek   |
| `qwen3-coder:latest`   | ~20 GB     | Newest Qwen coder release (MoE), strong agentic/tool-use coding |

Change the model anytime by updating `OLLAMA_MODEL` in `.env` and restarting.

### Troubleshooting: agents keep timing out (`RetryError`/`ReadTimeout` in logs)

The 4 specialist agents (code quality, security, performance, documentation)
default to running **strictly one at a time** in `cpu` execution mode
specifically because a typical single-GPU/CPU Ollama instance only processes
one generation at a time — firing requests concurrently just makes them
queue on Ollama's side while each one's own client-side timeout clock is
already running, so it can time out before Ollama even starts on it.
Symptoms if this ever regresses: 3 of 4 agents return 0 findings and log
`RetryError[...ReadTimeout]`, while only rule-based (non-LLM) findings ever
show up — e.g. the review always reports exactly the same 2 issues
regardless of what changed.

**Verify it's actually taking effect:** look for the `execution.mode_resolved`
log line at startup — it includes `resolved=` and `max_parallel_llm_requests=`.
If `resolved` is `gpu` on a box that can't actually parallelise Ollama
generation, either hardware detection found a GPU that Ollama isn't using
for this model (check `ollama_using_gpu` in the `hardware.detected` log line)
or `EXECUTION_MODE` is explicitly forced to `gpu` in `.env` — set it to `cpu`
to force serial execution regardless of detection. Also check
`coordinator.start`'s `agent_concurrency=` field matches. The process needs a
full restart after any `.env` change — `uvicorn --reload` watches `*.py`
changes, not env files.

If agents are confirmed running one at a time and a *specific* one (often
`code_quality` or `security` — they have the longest system prompts) still
times out, its prompt is simply taking longer than `OLLAMA_TIMEOUT` to
generate on your hardware — raise `OLLAMA_TIMEOUT` further (600+ for CPU-only
inference of larger coder models).

If you have spare GPU/cloud capacity and want agents running concurrently
again, set `EXECUTION_MODE=gpu` (or leave `auto` if detection finds your GPU)
and raise `MAX_PARALLEL_LLM_REQUESTS` accordingly.

---

## Project Structure

```
code-review-agent/
├── main.py                        # Entry point
├── .env.example                   # Configuration template
├── requirements.txt
├── Dockerfile
├── docker-compose.yml
├── setup/                          # Per-OS setup/run/service scripts (see "One-command setup" above)
│   ├── windows/                    # setup.ps1, run.ps1, install-service.ps1 (NSSM), uninstall-service.ps1
│   ├── linux/                      # setup.sh, run.sh, install-service.sh (systemd), uninstall-service.sh, code-review-agent.service
│   └── macos/                      # setup.sh, run.sh
├── app/
│   ├── settings.py                # Pydantic settings (reads .env)
│   ├── server.py                  # FastAPI app + webhook handler + dashboard API
│   ├── reporting.py               # Execution report: console table, structlog, reports/*.json
│   ├── static/
│   │   └── dashboard.html         # Self-contained dashboard (served at /dashboard)
│   ├── agents/
│   │   ├── base_agent.py          # Abstract base + JSON parser
│   │   ├── coordinator.py         # Orchestrator
│   │   ├── code_quality_agent.py
│   │   ├── security_agent.py
│   │   ├── performance_agent.py
│   │   └── documentation_agent.py
│   ├── llm/
│   │   └── ollama_client.py       # Async Ollama wrapper
│   ├── gitlab/
│   │   └── client.py              # GitLab API client + MR branch clone
│   ├── execution/
│   │   ├── hardware.py            # CPU/RAM/GPU + Ollama-GPU-usage detection
│   │   └── policy.py              # Resolves EXECUTION_MODE -> ExecutionPolicy
│   ├── static_analysis/
│   │   ├── language_detector.py   # File extension -> Language
│   │   ├── base_analyzer.py       # StaticAnalyzer plugin interface
│   │   ├── diagnostics.py         # DiagnosticIssue common model
│   │   ├── registry.py            # Language -> analyzer class plugin registry
│   │   ├── roslyn_analyzer.py     # C# (via `dotnet format analyzers`)
│   │   ├── eslint_analyzer.py     # JS / TS
│   │   ├── ruff_analyzer.py       # Python
│   │   ├── clippy_analyzer.py     # Rust
│   │   ├── clang_tidy_analyzer.py # C / C++
│   │   ├── spotbugs_analyzer.py   # Java
│   │   └── runner.py              # Dispatches by language, merges results
│   └── utils/
│       ├── models.py              # Pydantic data models
│       ├── diff_parser.py         # Unified diff parser
│       └── process_runner.py      # Shared async subprocess runner (analyzers + hardware detection)
├── tests/
│   ├── test_agents.py
│   ├── test_code_fixes.py         # applicable fixes: parsing, validation, GitLab suggestion rendering
│   ├── test_reporting.py          # report JSON payload + dashboard read-side (path traversal guard)
│   ├── test_coordinator.py
│   ├── test_diff_parser.py
│   ├── test_static_analysis.py
│   ├── test_ollama_client.py
│   ├── test_execution.py
│   ├── test_process_runner.py
│   └── review_local.py            # CLI for local testing
```
