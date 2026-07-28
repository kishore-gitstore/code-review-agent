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
  CoordinatorAgent
  ├── CodeQualityAgent   → bugs, logic errors, smells
  ├── SecurityAgent      → OWASP Top 10, secrets, injection
  ├── PerformanceAgent   → N+1 queries, blocking I/O, caching
  └── DocumentationAgent → missing docstrings, outdated comments
        │
        ▼
  Deduplicate + Prioritise
        │
        ▼
  LLM Summary Generation
        │
        ▼
  Post inline + summary comments → GitLab MR
```

---

## Prerequisites

| Tool   | Version  | Purpose                      |
|--------|----------|------------------------------|
| Python | ≥ 3.12   | Runtime                      |
| Ollama | latest   | Local LLM runner             |
| Docker | optional | Containerised deployment     |

---

## Quick Start

### 1. Install Ollama & pull DeepSeek

```bash
# macOS / Linux
curl -fsSL https://ollama.com/install.sh | sh

# Pull the model (choose one)
ollama pull deepseek-coder-v2      # recommended — 16GB RAM
ollama pull deepseek-r1:7b         # lighter — 8GB RAM
ollama pull codellama:13b          # alternative
```

### 2. Clone & configure

```bash
git clone https://github.com/your-org/code-review-agent.git
cd code-review-agent

cp .env.example .env
# Edit .env — set GITLAB_TOKEN and GITLAB_WEBHOOK_SECRET
```

### 3. Install Python dependencies

```bash
python -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### 4. Run the server

```bash
python main.py
# Server starts at http://localhost:8000
```

### 5. Configure GitLab Webhook

1. Go to your GitLab project → **Settings → Webhooks**
2. Set URL: `https://your-server.com/webhook/gitlab`
3. Set Secret Token: (same as `GITLAB_WEBHOOK_SECRET` in `.env`)
4. Enable trigger: ✅ **Merge request events**
5. Click **Add webhook**

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
  -d '{"project_id": "myorg/myrepo", "mr_iid": 42}'
```

---

## Configuration Reference (`.env`)

| Variable                  | Default                     | Description                              |
|---------------------------|-----------------------------|------------------------------------------|
| `OLLAMA_BASE_URL`         | `http://localhost:11434`    | Ollama API URL                           |
| `OLLAMA_MODEL`            | `deepseek-coder-v2`         | Model name (must be pulled first)        |
| `OLLAMA_TIMEOUT`          | `120`                       | Seconds before LLM call times out       |
| `GITLAB_URL`              | `https://gitlab.com`        | Your GitLab instance URL                 |
| `GITLAB_TOKEN`            | —                           | Personal/Project access token (**required**) |
| `GITLAB_WEBHOOK_SECRET`   | —                           | Webhook secret for signature verification|
| `MAX_DIFF_LINES`          | `2000`                      | Truncate diffs larger than this          |
| `MIN_SEVERITY_TO_COMMENT` | `low`                       | Only post issues at or above this level  |
| `POST_INLINE_COMMENTS`    | `true`                      | Post per-line diff comments              |
| `POST_SUMMARY_COMMENT`    | `true`                      | Post a top-level summary comment         |
| `ENABLE_CODE_QUALITY`     | `true`                      | Toggle Code Quality agent                |
| `ENABLE_SECURITY`         | `true`                      | Toggle Security agent                    |
| `ENABLE_PERFORMANCE`      | `true`                      | Toggle Performance agent                 |
| `ENABLE_DOCUMENTATION`    | `true`                      | Toggle Documentation agent               |

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

Change the model anytime by updating `OLLAMA_MODEL` in `.env` and restarting.

---

## Project Structure

```
code-review-agent/
├── main.py                        # Entry point
├── .env.example                   # Configuration template
├── requirements.txt
├── Dockerfile
├── docker-compose.yml
├── config/
│   ├── __init__.py
│   └── settings.py                # Pydantic settings
├── app/
│   ├── server.py                  # FastAPI app + webhook handler
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
│   │   └── client.py              # GitLab API client
│   └── utils/
│       ├── models.py              # Pydantic data models
│       └── diff_parser.py         # Unified diff parser
├── tests/
│   ├── test_agents.py
│   ├── test_coordinator.py
│   └── test_diff_parser.py
└── scripts/
    └── review_local.py            # CLI for local testing
```
