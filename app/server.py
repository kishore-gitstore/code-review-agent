"""
app/server.py — FastAPI application: webhook receiver + manual trigger endpoint.

Supports multiple concurrent MR reviews. Each review runs as a background task
so webhooks return 202 immediately (GitLab's 10-second timeout is never hit).
Duplicate reviews for the same MR are suppressed while one is already running.
Progress is queryable at GET /review/status/{project_id}/{mr_iid}, and a
running review can be stopped with POST /review/{...}/cancel.

Two audiences, two access rules:

  * GitLab (POST /webhook/gitlab/{instance}) authenticates with the shared
    webhook secret and is never behind the login — a hook can't sign in.
  * People (the dashboard, the report APIs, the manual trigger, cancel)
    go through require_user, i.e. an Active Directory session. See app/auth/.
"""

from __future__ import annotations
import asyncio
import shutil
import structlog
import tempfile
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional
from fastapi import FastAPI, Request, HTTPException, Header, Depends
from fastapi.responses import JSONResponse, FileResponse, RedirectResponse

from app import envconfig
from app.service_state import service
from app.settings import get_settings, reload_settings
from app.execution import detect_hardware, resolve_execution_policy, set_execution_policy
from app.llm.factory import get_llm_client, reset_client
from app.gitlab.client import GitLabClient, get_gitlab_client, UnknownGitLabInstance
from app.agents.coordinator import CoordinatorAgent
from app.auth import AuthError, AuthUser
from app.auth.deps import (clear_session_cookie, current_user, is_admin,
                           require_admin, require_user, safe_next,
                           set_session_cookie)
from app.auth.ldap_auth import authenticate
from app.reporting import ExecutionReport, list_reports, read_report
from app.utils.console import force_utf8_streams
from app.utils.models import MRContext
from app.utils.diff_parser import get_changed_files, get_path_map, parse_diff

log = structlog.get_logger(__name__)

_STATIC_DIR = Path(__file__).parent / "static"

# Gates how many MR reviews run through their full pipeline (GitLab fetch +
# static analysis + LLM agents + posting) at once. Sized from the resolved
# execution policy once lifespan() runs; defaults to 1 (safe/serial) so a
# review triggered before startup completes, or in a test that never runs
# lifespan, still behaves correctly.
_review_semaphore = asyncio.Semaphore(1)

# Live asyncio.Task per running review, keyed like _reviews. This is what
# makes cancellation possible at all: without a handle on the task, "stop
# this review" could only ever be a flag the pipeline chose to check between
# stages, which would leave a request that's 4 minutes into an LLM call
# running to completion anyway.
_tasks: dict[str, asyncio.Task] = {}

# Strong references to fire-and-forget tasks (the review runners themselves,
# and the post-cancellation MR update). asyncio only holds a weak reference to
# a running task, so without this set the garbage collector is free to destroy
# one mid-flight.
_detached: set[asyncio.Task] = set()


def _spawn_detached(coro) -> asyncio.Task:
    task = asyncio.create_task(coro)
    _detached.add(task)
    task.add_done_callback(_detached.discard)
    return task

# ── In-memory review progress tracker ────────────────────────────────────────

@dataclass
class ReviewProgress:
    instance:       str
    project_id:     str | int
    mr_iid:         int
    mr_title:       str = ""
    mr_author:      str = ""
    project_name:   str = ""
    branch:         str = ""
    status:         str = "running"        # running | done | error | cancelled
    started_at:     str = ""
    finished_at:    Optional[str] = None
    agents_all:     list = field(default_factory=list)
    agents_done:    list = field(default_factory=list)
    agents_running: list = field(default_factory=list)
    total_issues:   Optional[int] = None
    critical_count: Optional[int] = None
    high_count:     Optional[int] = None
    error:          Optional[str] = None
    # Finished, but part of it never ran (see ReviewResult.degraded) — so the
    # counts above are a floor and this must not be badged as a plain success.
    degraded:       bool = False
    # Who asked for this review ("" for a GitLab webhook, which nobody
    # personally triggered), and the cancellation trail.
    triggered_by:   str = ""
    cancel_requested: bool = False
    cancelled_by:   str = ""

    @property
    def agents_pending(self) -> list:
        done_or_running = set(self.agents_done) | set(self.agents_running)
        return [a for a in self.agents_all if a not in done_or_running]

    def as_dict(self) -> dict:
        return {
            "instance":       self.instance,
            "project_id":     self.project_id,
            "mr_iid":         self.mr_iid,
            "mr_title":       self.mr_title,
            "mr_author":      self.mr_author,
            "project_name":   self.project_name,
            "branch":         self.branch,
            "status":         self.status,
            "started_at":     self.started_at,
            "finished_at":    self.finished_at,
            "agents_total":   len(self.agents_all),
            "agents_done":    self.agents_done,
            "agents_running": self.agents_running,
            "agents_pending": self.agents_pending,
            "total_issues":   self.total_issues,
            "critical_count": self.critical_count,
            "high_count":     self.high_count,
            "error":          self.error,
            "degraded":       self.degraded,
            "triggered_by":   self.triggered_by,
            # Set the moment someone hits Cancel, while the task is still
            # unwinding — the dashboard shows "cancelling…" off this rather
            # than leaving the row looking untouched until it actually stops.
            "cancel_requested": self.cancel_requested,
            "cancelled_by":   self.cancelled_by,
        }


# Keyed by "{project_id}/{mr_iid}". Keeps the last _MAX_COMPLETED finished reviews.
_reviews: dict[str, ReviewProgress] = {}
_MAX_COMPLETED = 50


def _rkey(instance: str, project_id: str | int, mr_iid: int) -> str:
    return f"{instance}/{project_id}/{mr_iid}"


def _now() -> str:
    # Naive local time, matching reporting.py/coordinator.py's datetime.now()
    # usage — the dashboard's fmtTime() displays these strings verbatim with
    # no timezone conversion, so every timestamp source must agree on local
    # time or the "live" and "finished" rows show different clocks.
    return datetime.now().isoformat()


def _register(instance: str, project_id: str | int, mr_iid: int, mr_title: str,
              agent_names: list[str], triggered_by: str = "") -> bool:
    """Register a new review. Returns False if one is already running for this MR."""
    key = _rkey(instance, project_id, mr_iid)
    existing = _reviews.get(key)
    if existing and existing.status == "running":
        log.warning("review.duplicate_skipped", instance=instance, mr=mr_iid, project=project_id)
        return False
    _reviews[key] = ReviewProgress(
        instance=instance,
        project_id=project_id,
        mr_iid=mr_iid,
        mr_title=mr_title,
        status="running",
        started_at=_now(),
        agents_all=list(agent_names),
        triggered_by=triggered_by,
    )
    log.info("review.registered", instance=instance, mr=mr_iid, project=project_id, agents=agent_names)
    return True


def _on_agent_start(instance: str, project_id: str | int, mr_iid: int, agent: str) -> None:
    if p := _reviews.get(_rkey(instance, project_id, mr_iid)):
        if agent not in p.agents_running:
            p.agents_running.append(agent)
        log.info("review.agent.started", instance=instance, mr=mr_iid, agent=agent,
                 progress=f"{len(p.agents_done)}/{len(p.agents_all)}")


def _on_agent_done(instance: str, project_id: str | int, mr_iid: int, agent: str, findings: int) -> None:
    if p := _reviews.get(_rkey(instance, project_id, mr_iid)):
        if agent in p.agents_running:
            p.agents_running.remove(agent)
        if agent not in p.agents_done:
            p.agents_done.append(agent)
        log.info("review.agent.done", instance=instance, mr=mr_iid, agent=agent, findings=findings,
                 progress=f"{len(p.agents_done)}/{len(p.agents_all)}",
                 remaining=p.agents_pending)


def _finish(instance: str, project_id: str | int, mr_iid: int, total: int, critical: int,
            high: int, degraded: bool = False) -> None:
    if p := _reviews.get(_rkey(instance, project_id, mr_iid)):
        p.status = "done"
        p.finished_at = _now()
        p.total_issues = total
        p.critical_count = critical
        p.high_count = high
        p.degraded = degraded
        p.agents_running = []
        log.info("review.done", instance=instance, mr=mr_iid, total=total, critical=critical,
                 high=high, degraded=degraded)
    _trim()


def _fail(instance: str, project_id: str | int, mr_iid: int, error: str) -> None:
    if p := _reviews.get(_rkey(instance, project_id, mr_iid)):
        p.status = "error"
        p.finished_at = _now()
        p.error = error
        p.agents_running = []
    _trim()


def _mark_cancelled(instance: str, project_id: str | int, mr_iid: int) -> ReviewProgress | None:
    """
    Close out the tracker entry for a review that was stopped mid-flight.

    Returns the entry (so the caller can read cancelled_by / agent progress
    for the MR note) or None if it's already gone. Idempotent: called from
    both _run_review's cancellation handler and its outer wrapper, because a
    cancel can land either inside the pipeline or while it's still queued on
    the review semaphore.
    """
    p = _reviews.get(_rkey(instance, project_id, mr_iid))
    if p is None:
        return None
    if p.status == "running":
        p.status = "cancelled"
        p.finished_at = _now()
        p.error = f"Cancelled by {p.cancelled_by}" if p.cancelled_by else "Cancelled"
        p.agents_running = []
        log.warning("review.cancelled", instance=instance, mr=mr_iid, project=project_id,
                    by=p.cancelled_by or "unknown",
                    agents_done=len(p.agents_done), agents_total=len(p.agents_all))
    _trim()
    return p


def _trim() -> None:
    done_keys = [k for k, v in _reviews.items() if v.status in ("done", "error", "cancelled")]
    for k in done_keys[:-_MAX_COMPLETED]:
        del _reviews[k]


# ── Application lifecycle ─────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    cfg = get_settings()
    log.info("startup", provider=cfg.llm_provider, gitlab_instances=list(cfg.gitlab_instances.keys()))
    await _configure_runtime()
    yield
    log.info("shutdown")


async def _configure_runtime() -> None:
    """Build everything derived from settings: execution policy, the review
    semaphore and the LLM client. Run at startup, and again when the
    configuration page applies a saved change (only while no review is
    running — see apply_config)."""
    global _review_semaphore
    cfg = get_settings()

    # Hardware detection + execution-mode resolution must happen before the
    # LLM client singleton is constructed (get_llm_client(), just below) —
    # OllamaClient sizes its own concurrency semaphore from the resolved
    # policy at construction time.
    hardware = await detect_hardware(
        ollama_base_url=cfg.ollama_base_url if cfg.llm_provider == "ollama" else None,
        ollama_model=cfg.ollama_model if cfg.llm_provider == "ollama" else None,
    )
    policy = resolve_execution_policy(cfg, hardware)
    set_execution_policy(policy)
    _review_semaphore = asyncio.Semaphore(policy.max_parallel_reviews)
    log.info("execution.policy_active", mode=policy.mode,
             max_parallel_reviews=policy.max_parallel_reviews,
             max_parallel_analyzers=policy.max_parallel_analyzers,
             max_parallel_llm_requests=policy.max_parallel_llm_requests)

    health = await get_llm_client().health_check()
    if health["status"] != "ok":
        log.warning("llm.unavailable", detail=health)
    else:
        log.info("llm.ready", provider=health.get("provider"), model=health["model"])


def create_app() -> FastAPI:
    # Also done in main.py, but repeated here so the ASGI entry points
    # (`uvicorn main:app` in the Dockerfile, `--factory`) can't miss it.
    force_utf8_streams()

    app = FastAPI(
        title="Code Review Agent",
        description="Multi-MR concurrent code review agent",
        version="3.0.0",
        lifespan=lifespan,
    )

    # ── Health ────────────────────────────────────────────────────────────
    # Deliberately unauthenticated: this is what a load balancer, NSSM
    # wrapper or uptime probe hits, and none of those can hold a session. It
    # exposes no review data — only whether the LLM backend answers.
    @app.get("/health")
    async def health():
        llm = await get_llm_client().health_check()
        return {"status": llm["status"], "provider": llm.get("provider"),
                "model": llm.get("model")}

    # ── Authentication ────────────────────────────────────────────────────
    @app.get("/login")
    async def login_page(request: Request, next: str = "/"):
        """The sign-in form. Already signed in? Go straight through, so a
        bookmarked /login isn't a dead end."""
        if current_user(request) is not None:
            return RedirectResponse(safe_next(next), status_code=303)
        return FileResponse(_STATIC_DIR / "login.html")

    @app.get("/api/auth/info")
    async def auth_info():
        """What the login page needs before anyone types anything. Returns the
        AD domain (so the form can label the username field) and nothing that
        would help an attacker — no server address, no group names."""
        cfg = get_settings()
        return {"auth_enabled": cfg.auth_enabled, "domain": cfg.ldap_domain}

    @app.post("/api/login")
    async def api_login(body: dict):
        """Verify AD credentials and set the session cookie."""
        cfg = get_settings()
        if not cfg.auth_enabled:
            return JSONResponse({"status": "ok", "user": None, "auth_enabled": False})
        try:
            user = await authenticate(body.get("username", ""), body.get("password", ""))
        except AuthError as exc:
            # Each AuthError subclass carries its own status, so a directory
            # outage (503) isn't reported to the user as a bad password (401).
            raise HTTPException(status_code=exc.http_status, detail=exc.message)
        response = JSONResponse({"status": "ok", "user": user.as_dict()})
        set_session_cookie(response, user)
        return response

    @app.post("/api/logout")
    async def api_logout():
        response = JSONResponse({"status": "ok"})
        clear_session_cookie(response)
        return response

    @app.get("/api/me")
    async def api_me(request: Request):
        """Who the browser is signed in as. 401 rather than an empty body, so
        the dashboard's fetch wrapper treats it like every other gated call."""
        user = current_user(request)
        if user is None:
            raise HTTPException(status_code=401, detail="Not signed in")
        return {"user": user.as_dict(), "auth_enabled": get_settings().auth_enabled,
                "is_admin": is_admin(user)}

    # ── Home, configuration, service control ──────────────────────────────
    @app.get("/")
    async def home(request: Request):
        """Landing page after sign-in: buttons to the reports, the
        configuration page and the service switch."""
        if current_user(request) is None:
            return RedirectResponse("/login?next=/", status_code=303)
        return FileResponse(_STATIC_DIR / "home.html")

    @app.get("/config")
    async def config_page(request: Request):
        user = current_user(request)
        if user is None:
            return RedirectResponse("/login?next=/config", status_code=303)
        if not is_admin(user):
            return RedirectResponse("/?reason=admin", status_code=303)
        return FileResponse(_STATIC_DIR / "config.html")

    @app.get("/api/config")
    async def get_config(user: AuthUser = Depends(require_admin)):
        """Schema + current values. Secrets come back masked, never in clear."""
        return {"groups": envconfig.schema_payload(), "values": envconfig.masked_values(),
                "secret_mask": envconfig.SECRET_MASK}

    @app.put("/api/config")
    async def put_config(body: dict, user: AuthUser = Depends(require_admin)):
        """Write the changed settings to .env, then reload them live when it
        is safe to. Settings that rebuild the LLM client or the concurrency
        limits wait until no review is running, so a save can't pull the rug
        out from under one mid-flight."""
        values = body.get("values")
        if not isinstance(values, dict):
            raise HTTPException(status_code=400, detail="values must be an object")
        try:
            changed = envconfig.apply_changes({k: v for k, v in values.items()})
        except envconfig.ConfigError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        log.warning("config.saved", by=user.username, keys=changed)
        if not changed:
            return {"changed": [], "applied": True, "restart_required": [],
                    "message": "No changes to save."}
        result = await _apply_config(changed)
        return {"changed": changed, **result}

    @app.post("/api/config/apply")
    async def apply_config_route(user: AuthUser = Depends(require_admin)):
        """Retry applying a saved configuration that was deferred because a
        review was running."""
        return await _apply_config(list(envconfig.FIELDS))

    @app.get("/api/service")
    async def service_status(user: AuthUser = Depends(require_user)):
        running = [p for p in _reviews.values() if p.status == "running"]
        return {**service.as_dict(), "running_reviews": len(running),
                "can_control": is_admin(user)}

    @app.post("/api/service/stop")
    async def service_stop(user: AuthUser = Depends(require_admin)):
        service.set(False, user.label)
        log.warning("service.stopped", by=user.username)
        return await service_status(user)

    @app.post("/api/service/start")
    async def service_start(user: AuthUser = Depends(require_admin)):
        service.set(True, user.label)
        log.warning("service.started", by=user.username)
        return await service_status(user)

    # ── Progress endpoints ────────────────────────────────────────────────
    @app.get("/review/active")
    async def list_reviews(user: AuthUser = Depends(require_user)):
        """List all running and recently completed reviews."""
        return [p.as_dict() for p in _reviews.values()]

    # {project_id:path} because GitLab projects are addressed by path
    # ("group/repo"). The dashboard percent-encodes the slash, but ASGI hands
    # Starlette the DECODED path, so a plain {project_id} sees one segment too
    # many and misses this route — a 404 the live drawer reported as "this
    # review is no longer active". :path soaks up the slashes and still leaves
    # the trailing {mr_iid} segment to match.
    @app.get("/review/status/{instance}/{project_id:path}/{mr_iid}")
    async def get_review_status(instance: str, project_id: str, mr_iid: int,
                                user: AuthUser = Depends(require_user)):
        """Get live progress for a specific MR review."""
        key = _rkey(instance, project_id, mr_iid)
        if key not in _reviews:
            raise HTTPException(status_code=404, detail="No review found for this MR")
        return _reviews[key].as_dict()

    @app.post("/review/{instance}/{project_id:path}/{mr_iid}/cancel", status_code=202)
    async def cancel_review(instance: str, project_id: str, mr_iid: int,
                            user: AuthUser = Depends(require_user)):
        """
        Stop a review that's currently running and record that on the MR.

        Returns 202, not 200: cancellation is a request, not an instant. The
        task is interrupted at its next await — usually immediately, but an
        in-flight HTTP response from the LLM still has to unwind — and only
        then does the MR note get replaced. Poll /review/status to watch it
        land; `cancel_requested` flips to true right away.
        """
        key = _rkey(instance, project_id, mr_iid)
        p = _reviews.get(key)
        if p is None:
            raise HTTPException(status_code=404, detail="No review found for this MR")
        if p.status != "running":
            raise HTTPException(
                status_code=409,
                detail=f"This review is already {p.status} — there is nothing to cancel.",
            )

        # Attribute the cancellation before interrupting anything: the task's
        # own handler reads cancelled_by off this record to write the MR note,
        # and it can start running the moment task.cancel() is called.
        p.cancel_requested = True
        p.cancelled_by = user.label

        task = _tasks.get(key)
        if task is None or task.done():
            # Tracker says running but no live task owns it. That means the
            # record outlived its runner (the task died without unwinding
            # through its own handler), so nothing will clean up after it —
            # close the record out here and still tell the MR.
            log.warning("review.cancel_orphaned", instance=instance, mr=mr_iid, project=project_id)
            _mark_cancelled(instance, project_id, mr_iid)
            _spawn_detached(_announce_cancellation(instance, project_id, mr_iid, p))
            return {"status": "cancelled", "detail": "No running task was found; the record was closed out.",
                    "progress": p.as_dict()}

        log.warning("review.cancel_requested", instance=instance, mr=mr_iid,
                    project=project_id, by=user.username)
        task.cancel()
        return {"status": "cancelling", "progress": p.as_dict()}

    @app.post("/review/{instance}/{project_id:path}/{mr_iid}/clear-stale-note")
    async def clear_stale_note(instance: str, project_id: str, mr_iid: int,
                               user: AuthUser = Depends(require_user)):
        """
        Force-delete a dangling "review in progress" note right now, without
        waiting for the next push to this MR to auto-clean it (see
        delete_stale_progress_notes() in _run_review). Refuses while this
        process itself still thinks a review is running for this MR — that's
        very likely just a slow review, not a stuck one; check
        /review/status or the service logs first.
        """
        key = _rkey(instance, project_id, mr_iid)
        if (p := _reviews.get(key)) and p.status == "running":
            raise HTTPException(
                status_code=409,
                detail="A review is currently marked running for this MR — check /review/status "
                       "or the logs before force-clearing; this looks like it's still in progress, not stuck.",
            )
        try:
            gitlab = get_gitlab_client(instance)
        except UnknownGitLabInstance as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        deleted = await gitlab.delete_stale_progress_notes(project_id, mr_iid)
        return {"deleted": deleted}

    # ── Dashboard ─────────────────────────────────────────────────────────
    @app.get("/dashboard")
    async def dashboard(request: Request):
        """Serves the static dashboard page; it fetches /api/reports itself.

        A signed-out browser is redirected to the login form rather than shown
        a bare 401 — this is a page a human typed into the address bar, and
        the page itself carries no report data, so bouncing it is safe.
        """
        if current_user(request) is None:
            return RedirectResponse("/login?next=/dashboard", status_code=303)
        return FileResponse(_STATIC_DIR / "dashboard.html")

    @app.get("/api/reports")
    async def api_list_reports(limit: int = 200, user: AuthUser = Depends(require_user)):
        """Execution-summary list backing the dashboard table — one entry per
        completed review (local CLI or GitLab-triggered), newest first."""
        return list_reports(limit=limit)

    @app.get("/api/reports/{filename}")
    async def api_get_report(filename: str, user: AuthUser = Depends(require_user)):
        """Full stats (including the per-LLM-request table) for one report."""
        try:
            return read_report(filename)
        except ValueError:
            raise HTTPException(status_code=400, detail="invalid report filename")
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="report not found")

    # ── GitLab Webhook ────────────────────────────────────────────────────
    # Not behind require_user: GitLab can't hold a session. It authenticates
    # with the per-instance webhook secret checked below instead.
    @app.post("/webhook/gitlab/{instance}", status_code=202)
    async def gitlab_webhook(
        instance: str,
        request: Request,
        x_gitlab_token: str | None = Header(default=None),
        x_gitlab_event: str | None = Header(default=None),
    ):
        await request.body()  # consume body before json()

        try:
            gitlab = get_gitlab_client(instance)
        except UnknownGitLabInstance as exc:
            raise HTTPException(status_code=404, detail=str(exc))

        if not gitlab.verify_webhook(x_gitlab_token or ""):
            raise HTTPException(status_code=401, detail="Invalid webhook token")

        if x_gitlab_event != "Merge Request Hook":
            return JSONResponse({"status": "ignored", "event": x_gitlab_event})

        # Acknowledged rather than refused: a run of non-2xx answers makes
        # GitLab disable the webhook, and a deliberate pause shouldn't cost
        # an admin a trip to every project's settings afterwards.
        if not service.running:
            log.warning("webhook.ignored_service_stopped", instance=instance)
            return JSONResponse({"status": "ignored", "reason": "service stopped"})

        payload    = await request.json()
        attrs      = payload.get("object_attributes", {})
        action     = attrs.get("action", "")

        if action not in ("open", "reopen", "update"):
            return JSONResponse({"status": "ignored", "action": action})

        project    = payload.get("project", {})
        project_id = project.get("id")
        mr_iid     = attrs.get("iid")
        mr_title   = attrs.get("title", "")

        if not project_id or not mr_iid:
            raise HTTPException(status_code=400, detail="Missing project_id or mr_iid")

        log.info("webhook.mr_event", instance=instance, project=project_id, mr=mr_iid, action=action)

        # Suppress duplicate if already running
        existing = _reviews.get(_rkey(instance, project_id, mr_iid))
        if existing and existing.status == "running":
            log.warning("webhook.duplicate_ignored", instance=instance, mr=mr_iid, project=project_id)
            return JSONResponse({
                "status": "already_in_progress",
                "mr_iid": mr_iid,
                "progress": existing.as_dict(),
            })

        # asyncio.create_task rather than BackgroundTasks: cancellation needs a
        # handle on the task, and BackgroundTasks keeps none. The runner
        # publishes itself into _tasks as its first act.
        _spawn_detached(_run_review_bg(instance, project_id, mr_iid, mr_title))
        return JSONResponse({
            "status": "accepted",
            "mr_iid": mr_iid,
            "status_url": f"/review/status/{instance}/{project_id}/{mr_iid}",
            "cancel_url": f"/review/{instance}/{project_id}/{mr_iid}/cancel",
        }, status_code=202)

    # ── Manual trigger ────────────────────────────────────────────────────
    @app.post("/review", status_code=202)
    async def manual_review(body: dict, user: AuthUser = Depends(require_user)):
        """
        Manually trigger a review.
        POST /review  { "instance": "gitlab_com", "project_id": "group/repo", "mr_iid": 42 }
        "instance" can be omitted only if exactly one is configured in GITLAB_INSTANCES.
        Returns 202 immediately; poll GET /review/status/{instance}/{project_id}/{mr_iid} for progress.

        Needs a signed-in user or an X-API-Token header — unlike the webhook,
        this endpoint has no shared secret of its own, and left open it would
        let anyone who can reach the port spend the review budget.
        """
        if not service.running:
            raise HTTPException(status_code=503, detail="The review service is stopped. An administrator can start it from the home page.")
        cfg        = get_settings()
        instance   = body.get("instance")
        project_id = body.get("project_id")
        mr_iid     = body.get("mr_iid")
        if not project_id or not mr_iid:
            raise HTTPException(status_code=400, detail="project_id and mr_iid required")

        if not instance:
            if len(cfg.gitlab_instances) == 1:
                instance = next(iter(cfg.gitlab_instances))
            else:
                known = ", ".join(cfg.gitlab_instances) or "(none configured — check GITLAB_INSTANCES in .env)"
                raise HTTPException(status_code=400, detail=f"instance required — one of: {known}")
        try:
            get_gitlab_client(instance)
        except UnknownGitLabInstance as exc:
            raise HTTPException(status_code=404, detail=str(exc))

        mr_iid = int(mr_iid)
        existing = _reviews.get(_rkey(instance, project_id, mr_iid))
        if existing and existing.status == "running":
            return JSONResponse({
                "status": "already_in_progress",
                "mr_iid": mr_iid,
                "progress": existing.as_dict(),
            })

        _spawn_detached(_run_review_bg(instance, project_id, mr_iid, "", user.label))
        return JSONResponse({
            "status": "accepted",
            "mr_iid": mr_iid,
            "status_url": f"/review/status/{instance}/{project_id}/{mr_iid}",
            "cancel_url": f"/review/{instance}/{project_id}/{mr_iid}/cancel",
        }, status_code=202)

    return app


async def _apply_config(changed: list[str]) -> dict:
    """Make a just-saved .env take effect in the running process.

    Settings, GitLab clients and the session serializer are refreshed
    straight away. Rebuilding the LLM client and the concurrency limits is
    held back while a review is running: they're shared by every in-flight
    review, and swapping them mid-run would change the rules underneath one.
    """
    from app.gitlab import client as gitlab_client_mod
    from app.auth import session as session_mod

    restart = [k for k in changed if k in envconfig.FIELDS and envconfig.FIELDS[k].restart]
    try:
        reload_settings()
    except Exception as exc:
        log.error("config.reload_failed", error=str(exc))
        return {"applied": False, "restart_required": restart,
                "message": f"Saved, but the new settings could not be loaded: {exc}"}
    gitlab_client_mod._clients.clear()
    if "SESSION_SECRET" in changed:
        session_mod.reset_serializer()

    running = [p for p in _reviews.values() if p.status == "running"]
    if running:
        return {"applied": False, "restart_required": restart,
                "message": f"Saved. {len(running)} review(s) are running, so the LLM and "
                           "concurrency settings will apply when they finish — press Apply then."}
    reset_client()
    try:
        await _configure_runtime()
    except Exception as exc:
        log.error("config.apply_failed", error=str(exc))
        return {"applied": False, "restart_required": restart,
                "message": f"Saved, but applying failed: {exc}. Restart the service."}
    msg = "Saved and applied."
    if restart:
        msg += " Host, port and log level need a service restart to take effect."
    return {"applied": True, "restart_required": restart, "message": msg}


# ── Background review runner ──────────────────────────────────────────────────

async def _announce_cancellation(instance: str, project_id: str | int, mr_iid: int,
                                 p: Optional[ReviewProgress]) -> None:
    """
    Replace the MR's "review in progress" placeholder with a permanent
    "review cancelled" record.

    Runs as its own detached task, never inline in the review it reports on:
    by the time this is scheduled the review task is already unwinding under
    CancelledError, and awaiting two GitLab round trips inside a task that is
    being torn down is exactly how a cancellation ends up half-applied — note
    deleted, replacement never posted.
    """
    try:
        gitlab = get_gitlab_client(instance)
    except UnknownGitLabInstance as exc:
        log.warning("review.cancel_note.unknown_instance", instance=instance, error=str(exc))
        return

    # Find the placeholder by its marker rather than by the note id the review
    # task was holding: a cancel can land while the review is still queued on
    # the semaphore, before that id was ever assigned. Matching on the marker
    # cleans up both cases the same way, and is a no-op when there's no note.
    try:
        await gitlab.delete_stale_progress_notes(project_id, mr_iid)
    except Exception as exc:
        log.warning("review.cancel_note.progress_cleanup_failed", mr=mr_iid, error=str(exc))

    duration_s = None
    if p and p.started_at:
        try:
            duration_s = (datetime.now() - datetime.fromisoformat(p.started_at)).total_seconds()
        except ValueError:
            pass

    try:
        await gitlab.post_cancelled_note(
            project_id, mr_iid,
            cancelled_by=(p.cancelled_by if p else "") or "a dashboard user",
            agents_done=len(p.agents_done) if p else None,
            agents_total=len(p.agents_all) if p else None,
            duration_s=duration_s,
        )
        log.info("review.cancel_note.posted", instance=instance, mr=mr_iid, project=project_id)
    except Exception as exc:
        # The review is stopped either way; failing to say so on the MR is
        # bad but not worth retrying into a loop. The dashboard and the saved
        # report still carry the record.
        log.error("review.cancel_note.failed", instance=instance, mr=mr_iid,
                  project=project_id, error=str(exc))


def _stub_cancelled_report(instance: str, project_id: str | int, mr_iid: int,
                           mr_title: str, started_at: datetime) -> ExecutionReport:
    """
    A minimal report for a review cancelled before it fetched anything.

    The dashboard's table is fed entirely from reports/, and the in-memory
    tracker keeps only the last _MAX_COMPLETED entries — so without this,
    cancelling a review while it was still queued would leave no trace at all
    once the tracker rolled over.
    """
    report = ExecutionReport(
        repository=str(project_id), branch="", instance=instance,
        project_id=project_id, project_name=str(project_id),
        mr_iid=mr_iid, mr_title=mr_title,
    )
    report.started_at = started_at
    return report


async def _run_review_bg(instance: str, project_id: str | int, mr_iid: int,
                         mr_title: str, triggered_by: str = "") -> None:
    """Background entry-point — wraps _run_review with error handling."""
    try:
        await _run_review(instance, project_id, mr_iid, mr_title, triggered_by)
    except asyncio.CancelledError:
        # _run_review handles its own cancellation and re-raises; this is the
        # backstop for a cancel that lands somewhere it can't — and it's
        # idempotent, so the two can't double-report.
        _mark_cancelled(instance, project_id, mr_iid)
        raise
    except Exception as exc:
        log.error("review.failed", instance=instance, mr=mr_iid, project=project_id, error=str(exc))
        _fail(instance, project_id, mr_iid, str(exc))


async def _run_review(instance: str, project_id: str | int, mr_iid: int, mr_title: str = "",
                      triggered_by: str = "") -> None:
    """Register progress → fetch MR → run agents → post comments → mark done.

    Registration happens synchronously, before any `await`, so two background
    tasks racing for the same (project_id, mr_iid) — e.g. GitLab firing several
    webhook events for one MR update in quick succession — can't both slip past
    the duplicate check. Whichever task's coroutine starts running first claims
    the slot before it ever yields to the event loop; the other sees "running"
    immediately and bails out without hitting the GitLab API at all.

    Cancellation (POST /review/{...}/cancel) arrives as CancelledError at
    whatever this task is awaiting. Everything after the registration is
    therefore inside one try/finally, so a review stopped during the GitLab
    fetch is cleaned up exactly as thoroughly as one stopped mid-LLM-call.
    """
    coordinator = CoordinatorAgent()
    agent_names = [a.name for a in coordinator.agents]

    if not _register(instance, project_id, mr_iid, mr_title, agent_names, triggered_by):
        return  # duplicate — already running

    # Publish the task handle so the cancel endpoint has something to
    # interrupt. Immediately after _register and before any await, so the
    # window where the tracker says "running" with no cancellable task behind
    # it is closed — and after it, so the loser of a duplicate race can't
    # overwrite the winner's handle with its own about-to-return task.
    key = _rkey(instance, project_id, mr_iid)
    _tasks[key] = asyncio.current_task()

    # Everything from here on is the expensive part (GitLab fetch, cloning,
    # static analysis, LLM agents, posting) — gated by max_parallel_reviews.
    # Registration above stays outside the gate so the duplicate-suppression
    # race-safety guarantee (see docstring) holds regardless of queue depth.
    queued_at = time.monotonic()

    # Hoisted out of the gated block so the finally can always see them, no
    # matter how early the run was interrupted.
    gitlab: Optional[GitLabClient] = None
    report: Optional[ExecutionReport] = None
    progress_note_id: Optional[int] = None
    clone_dir: Optional[str] = None
    review_started_at = datetime.now()
    cancelled = False

    try:
        async with _review_semaphore:
            queue_wait_ms = int((time.monotonic() - queued_at) * 1000)
            if queue_wait_ms > 0:
                log.info("review.dequeued", instance=instance, mr=mr_iid, project=project_id, queue_wait_ms=queue_wait_ms)

            # Covers the whole gated review — GitLab fetch/clone included, not
            # just the coordinator's LLM/static-analysis stage — so the report's
            # Total Duration matches what a human waited for end to end.
            review_started_at = datetime.now()

            cfg    = get_settings()
            gitlab = get_gitlab_client(instance)

            # Clean up any "in progress" note a previous run left dangling — e.g.
            # the process crashed/restarted mid-review, so its own try/finally
            # cleanup below never got to run. Safe: the duplicate-check in
            # _register() above already guarantees no OTHER run is currently
            # active for this MR right now, so any marker note found here can
            # only be a stale leftover, never a currently-live one.
            try:
                cleaned = await gitlab.delete_stale_progress_notes(project_id, mr_iid)
                if cleaned:
                    log.warning("review.stale_progress_note.cleaned", mr=mr_iid, count=cleaned)
            except Exception as exc:
                log.warning("review.stale_progress_note.cleanup_failed", mr=mr_iid, error=str(exc))

            # Transient "review in progress" note — gives immediate feedback on the
            # MR while the (potentially slow, LLM-bound) review runs. Deleted once
            # real results are ready to post; see the finally block below for the
            # failure path, where it's also cleaned up rather than left dangling.
            if cfg.post_progress_comment:
                try:
                    progress_note_id = await gitlab.post_progress_note(project_id, mr_iid)
                except Exception as exc:
                    log.warning("review.progress_note.failed", mr=mr_iid, error=str(exc))

            mr_details = await gitlab.get_mr_details(project_id, mr_iid)
            diff       = await gitlab.get_mr_diff(project_id, mr_iid)
            mr_title   = mr_title or mr_details.get("title", "")
            mr_author_info = mr_details.get("author", {}) or {}
            mr_author  = mr_author_info.get("name") or mr_author_info.get("username", "unknown")

            if p := _reviews.get(key):
                p.mr_title  = mr_title
                p.mr_author = mr_author

            source_branch = mr_details.get("source_branch", "")
            head_sha      = (mr_details.get("diff_refs") or {}).get("head_sha", "")
            changed_files = get_changed_files(diff)
            file_diffs    = parse_diff(diff)

            if p := _reviews.get(key):
                p.branch = source_branch

            try:
                project      = await gitlab.get_project(project_id)
                project_name = project.get("path_with_namespace") or project.get("name") or str(project_id)
            except Exception as exc:
                log.warning("review.project_lookup_failed", project=project_id, error=str(exc))
                project_name = str(project_id)

            if p := _reviews.get(key):
                p.project_name = project_name

            report = ExecutionReport(
                repository=project_name, branch=source_branch, instance=instance,
                project_id=project_id, project_name=project_name, mr_iid=mr_iid, mr_title=mr_title,
                mr_author=mr_author, mr_url=mr_details.get("web_url", "") or "",
            )
            # Stamped as soon as the report exists, not just before the agents
            # run: a review cancelled or failed during the clone still needs a
            # start time, or it saves under a filename stamped "unknown" and
            # reports a zero duration.
            report.started_at    = review_started_at
            report.files_changed = len(changed_files)
            report.lines_added   = sum(fd.added_lines for fd in file_diffs)
            report.lines_removed = sum(fd.removed_lines for fd in file_diffs)

            repo_root: Optional[str] = None
            repo_root_note: Optional[str] = None

            if not cfg.enable_static_analysis:
                pass  # coordinator won't run the static stage at all — no note needed
            elif not cfg.clone_for_static_analysis:
                repo_root_note = "static analysis skipped — CLONE_FOR_STATIC_ANALYSIS is disabled"
            elif not source_branch:
                repo_root_note = "static analysis skipped — MR has no source branch"
            else:
                clone_dir = tempfile.mkdtemp(prefix=f"cra-mr{mr_iid}-")
                cloned, reason = await gitlab.clone_mr_source(project_id, source_branch, clone_dir)
                if cloned:
                    repo_root = clone_dir
                elif head_sha and changed_files:
                    # Full clone failed (no git, network issue, auth problem, ...) — fall
                    # back to fetching just the changed files via the GitLab API. Lighter
                    # and doesn't need `git`, but tools needing whole-project context
                    # (Roslyn, Clippy) will report themselves unavailable in this mode.
                    fetched, fetch_reason = await gitlab.fetch_changed_files(
                        project_id, changed_files, head_sha, clone_dir
                    )
                    if fetched:
                        repo_root = clone_dir
                    else:
                        shutil.rmtree(clone_dir, ignore_errors=True)
                        clone_dir = None
                        repo_root_note = f"static analysis skipped — clone failed ({reason}); file fetch also failed ({fetch_reason})"
                else:
                    shutil.rmtree(clone_dir, ignore_errors=True)
                    clone_dir = None
                    repo_root_note = f"static analysis skipped — {reason}"

            ctx = MRContext(
                project_id=project_id,
                mr_iid=mr_iid,
                title=mr_title,
                description=mr_details.get("description", "") or "",
                author=mr_details.get("author", {}).get("username", "unknown"),
                source_branch=source_branch,
                target_branch=mr_details.get("target_branch", ""),
                diff=diff,
                changed_files=changed_files,
                repo_root=repo_root,
                repo_root_note=repo_root_note,
            )

            result = await coordinator.run(
                ctx,
                on_agent_start=lambda agent: _on_agent_start(instance, project_id, mr_iid, agent),
                on_agent_done=lambda agent, n: _on_agent_done(instance, project_id, mr_iid, agent, n),
                on_llm_request=report.on_llm_request,
                on_analyzer_done=report.on_analyzer_done,
                on_phase=report.on_phase,
            )
            report.result = result
            report.status = "success"

            if progress_note_id is not None:
                try:
                    await gitlab.delete_mr_note(project_id, mr_iid, progress_note_id)
                except Exception as exc:
                    log.warning("review.progress_note.delete_failed", mr=mr_iid, error=str(exc))
                progress_note_id = None

            await gitlab.post_review(result, mr_details, get_path_map(diff))
            _finish(instance, project_id, mr_iid, result.total_issues, result.critical_count,
                    result.high_count, degraded=result.degraded)

    except asyncio.CancelledError:
        cancelled = True
        p = _mark_cancelled(instance, project_id, mr_iid)
        if report is None:
            # Stopped before the MR was even fetched (queued, or mid-fetch).
            report = _stub_cancelled_report(instance, project_id, mr_iid, mr_title, review_started_at)
        report.status = "cancelled"
        report.error  = (f"Cancelled by {p.cancelled_by}" if p and p.cancelled_by
                         else "Cancelled while in progress")
        # The MR update is handed to a detached task (see _announce_cancellation)
        # and it deletes the placeholder by marker, so the finally below must
        # not also try to delete it by id — that would race, and the loser
        # logs a spurious failure against a note that's already gone.
        progress_note_id = None
        _spawn_detached(_announce_cancellation(instance, project_id, mr_iid, p))
        raise

    except Exception as exc:
        if report is not None:
            report.status = "failed"
            report.error = str(exc)
        raise

    finally:
        _tasks.pop(key, None)
        if report is not None:
            report.finished_at = datetime.now()
            # Saves before it renders, and won't let a rendering failure cost
            # us the report the dashboard reads — see ExecutionReport.
            report.persist_and_render()

        # Also cleaned up on the failure path (coordinator/post_review raised)
        # so a botched review doesn't leave the placeholder note dangling.
        # Skipped when cancelled: awaiting GitLab inside a task that's already
        # unwinding is unreliable, so _announce_cancellation owns it instead.
        if progress_note_id is not None and gitlab is not None and not cancelled:
            try:
                await gitlab.delete_mr_note(project_id, mr_iid, progress_note_id)
            except Exception as exc:
                log.warning("review.progress_note.delete_failed", mr=mr_iid, error=str(exc))
        if clone_dir:
            shutil.rmtree(clone_dir, ignore_errors=True)
