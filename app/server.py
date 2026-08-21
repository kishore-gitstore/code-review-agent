"""
app/server.py — FastAPI application: webhook receiver + manual trigger endpoint.

Supports multiple concurrent MR reviews. Each review runs as a background task
so webhooks return 202 immediately (GitLab's 10-second timeout is never hit).
Duplicate reviews for the same MR are suppressed while one is already running.
Progress is queryable at GET /review/status/{project_id}/{mr_iid}.
"""

from __future__ import annotations
import asyncio
import shutil
import structlog
import tempfile
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from fastapi import FastAPI, Request, HTTPException, Header, BackgroundTasks
from fastapi.responses import JSONResponse, FileResponse

from app.settings import get_settings
from app.execution import detect_hardware, resolve_execution_policy, set_execution_policy
from app.llm.factory import get_llm_client
from app.gitlab.client import get_gitlab_client, UnknownGitLabInstance
from app.agents.coordinator import CoordinatorAgent
from app.reporting import ExecutionReport, list_reports, read_report
from app.utils.models import MRContext
from app.utils.diff_parser import get_changed_files, get_path_map, parse_diff

log = structlog.get_logger(__name__)

# Gates how many MR reviews run through their full pipeline (GitLab fetch +
# static analysis + LLM agents + posting) at once. Sized from the resolved
# execution policy once lifespan() runs; defaults to 1 (safe/serial) so a
# review triggered before startup completes, or in a test that never runs
# lifespan, still behaves correctly.
_review_semaphore = asyncio.Semaphore(1)

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
    status:         str = "running"        # running | done | error
    started_at:     str = ""
    finished_at:    Optional[str] = None
    agents_all:     list = field(default_factory=list)
    agents_done:    list = field(default_factory=list)
    agents_running: list = field(default_factory=list)
    total_issues:   Optional[int] = None
    critical_count: Optional[int] = None
    high_count:     Optional[int] = None
    error:          Optional[str] = None

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
        }


# Keyed by "{project_id}/{mr_iid}". Keeps the last _MAX_COMPLETED finished reviews.
_reviews: dict[str, ReviewProgress] = {}
_MAX_COMPLETED = 50


def _rkey(instance: str, project_id: str | int, mr_iid: int) -> str:
    return f"{instance}/{project_id}/{mr_iid}"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _register(instance: str, project_id: str | int, mr_iid: int, mr_title: str, agent_names: list[str]) -> bool:
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


def _finish(instance: str, project_id: str | int, mr_iid: int, total: int, critical: int, high: int) -> None:
    if p := _reviews.get(_rkey(instance, project_id, mr_iid)):
        p.status = "done"
        p.finished_at = _now()
        p.total_issues = total
        p.critical_count = critical
        p.high_count = high
        p.agents_running = []
        log.info("review.done", instance=instance, mr=mr_iid, total=total, critical=critical, high=high)
    _trim()


def _fail(instance: str, project_id: str | int, mr_iid: int, error: str) -> None:
    if p := _reviews.get(_rkey(instance, project_id, mr_iid)):
        p.status = "error"
        p.finished_at = _now()
        p.error = error
        p.agents_running = []
    _trim()


def _trim() -> None:
    done_keys = [k for k, v in _reviews.items() if v.status in ("done", "error")]
    for k in done_keys[:-_MAX_COMPLETED]:
        del _reviews[k]


# ── Application lifecycle ─────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _review_semaphore
    cfg = get_settings()
    log.info("startup", provider=cfg.llm_provider, gitlab_instances=list(cfg.gitlab_instances.keys()))

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
    yield
    log.info("shutdown")


def create_app() -> FastAPI:
    app = FastAPI(
        title="Code Review Agent",
        description="Multi-MR concurrent code review agent",
        version="3.0.0",
        lifespan=lifespan,
    )

    # ── Health ────────────────────────────────────────────────────────────
    @app.get("/health")
    async def health():
        llm = await get_llm_client().health_check()
        return {"status": llm["status"], "provider": llm.get("provider"),
                "model": llm.get("model")}

    # ── Progress endpoints ────────────────────────────────────────────────
    @app.get("/review/active")
    async def list_reviews():
        """List all running and recently completed reviews."""
        return [p.as_dict() for p in _reviews.values()]

    @app.get("/review/status/{instance}/{project_id}/{mr_iid}")
    async def get_review_status(instance: str, project_id: str, mr_iid: int):
        """Get live progress for a specific MR review."""
        key = _rkey(instance, project_id, mr_iid)
        if key not in _reviews:
            raise HTTPException(status_code=404, detail="No review found for this MR")
        return _reviews[key].as_dict()

    @app.post("/review/{instance}/{project_id}/{mr_iid}/clear-stale-note")
    async def clear_stale_note(instance: str, project_id: str, mr_iid: int):
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
    async def dashboard():
        """Serves the static dashboard page; it fetches /api/reports itself."""
        return FileResponse(Path(__file__).parent / "static" / "dashboard.html")

    @app.get("/api/reports")
    async def api_list_reports(limit: int = 200):
        """Execution-summary list backing the dashboard table — one entry per
        completed review (local CLI or GitLab-triggered), newest first."""
        return list_reports(limit=limit)

    @app.get("/api/reports/{filename}")
    async def api_get_report(filename: str):
        """Full stats (including the per-LLM-request table) for one report."""
        try:
            return read_report(filename)
        except ValueError:
            raise HTTPException(status_code=400, detail="invalid report filename")
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="report not found")

    # ── GitLab Webhook ────────────────────────────────────────────────────
    @app.post("/webhook/gitlab/{instance}", status_code=202)
    async def gitlab_webhook(
        instance: str,
        request: Request,
        background_tasks: BackgroundTasks,
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

        background_tasks.add_task(_run_review_bg, instance, project_id, mr_iid, mr_title)
        return JSONResponse({
            "status": "accepted",
            "mr_iid": mr_iid,
            "status_url": f"/review/status/{instance}/{project_id}/{mr_iid}",
        }, status_code=202)

    # ── Manual trigger ────────────────────────────────────────────────────
    @app.post("/review", status_code=202)
    async def manual_review(body: dict, background_tasks: BackgroundTasks):
        """
        Manually trigger a review.
        POST /review  { "instance": "gitlab_com", "project_id": "group/repo", "mr_iid": 42 }
        "instance" can be omitted only if exactly one is configured in GITLAB_INSTANCES.
        Returns 202 immediately; poll GET /review/status/{instance}/{project_id}/{mr_iid} for progress.
        """
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

        background_tasks.add_task(_run_review_bg, instance, project_id, mr_iid, "")
        return JSONResponse({
            "status": "accepted",
            "mr_iid": mr_iid,
            "status_url": f"/review/status/{instance}/{project_id}/{mr_iid}",
        }, status_code=202)

    return app


# ── Background review runner ──────────────────────────────────────────────────

async def _run_review_bg(instance: str, project_id: str | int, mr_iid: int, mr_title: str) -> None:
    """Background entry-point — wraps _run_review with error handling."""
    try:
        await _run_review(instance, project_id, mr_iid, mr_title)
    except Exception as exc:
        log.error("review.failed", instance=instance, mr=mr_iid, project=project_id, error=str(exc))
        _fail(instance, project_id, mr_iid, str(exc))


async def _run_review(instance: str, project_id: str | int, mr_iid: int, mr_title: str = "") -> None:
    """Register progress → fetch MR → run agents → post comments → mark done.

    Registration happens synchronously, before any `await`, so two background
    tasks racing for the same (project_id, mr_iid) — e.g. GitLab firing several
    webhook events for one MR update in quick succession — can't both slip past
    the duplicate check. Whichever task's coroutine starts running first claims
    the slot before it ever yields to the event loop; the other sees "running"
    immediately and bails out without hitting the GitLab API at all.
    """
    coordinator = CoordinatorAgent()
    agent_names = [a.name for a in coordinator.agents]

    if not _register(instance, project_id, mr_iid, mr_title, agent_names):
        return  # duplicate — already running

    # Everything from here on is the expensive part (GitLab fetch, cloning,
    # static analysis, LLM agents, posting) — gated by max_parallel_reviews.
    # Registration above stays outside the gate so the duplicate-suppression
    # race-safety guarantee (see docstring) holds regardless of queue depth.
    queued_at = time.monotonic()
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
        progress_note_id: Optional[int] = None
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

        if p := _reviews.get(_rkey(instance, project_id, mr_iid)):
            p.mr_title  = mr_title
            p.mr_author = mr_author

        source_branch = mr_details.get("source_branch", "")
        head_sha      = (mr_details.get("diff_refs") or {}).get("head_sha", "")
        changed_files = get_changed_files(diff)
        file_diffs    = parse_diff(diff)

        if p := _reviews.get(_rkey(instance, project_id, mr_iid)):
            p.branch = source_branch

        try:
            project      = await gitlab.get_project(project_id)
            project_name = project.get("path_with_namespace") or project.get("name") or str(project_id)
        except Exception as exc:
            log.warning("review.project_lookup_failed", project=project_id, error=str(exc))
            project_name = str(project_id)

        if p := _reviews.get(_rkey(instance, project_id, mr_iid)):
            p.project_name = project_name

        report = ExecutionReport(
            repository=project_name, branch=source_branch, instance=instance,
            project_id=project_id, project_name=project_name, mr_iid=mr_iid, mr_title=mr_title,
            mr_author=mr_author,
        )
        report.files_changed = len(changed_files)
        report.lines_added   = sum(fd.added_lines for fd in file_diffs)
        report.lines_removed = sum(fd.removed_lines for fd in file_diffs)

        repo_root: Optional[str] = None
        repo_root_note: Optional[str] = None
        clone_dir: Optional[str] = None

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

        try:
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

            report.started_at = review_started_at
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
            _finish(instance, project_id, mr_iid, result.total_issues, result.critical_count, result.high_count)
        except Exception as exc:
            report.status = "failed"
            report.error = str(exc)
            raise
        finally:
            report.finished_at = datetime.now()
            report.print_console()
            report.log_summary()
            report.save_json()

            # Also cleaned up on the failure path (coordinator/post_review raised)
            # so a botched review doesn't leave the placeholder note dangling.
            if progress_note_id is not None:
                try:
                    await gitlab.delete_mr_note(project_id, mr_iid, progress_note_id)
                except Exception as exc:
                    log.warning("review.progress_note.delete_failed", mr=mr_iid, error=str(exc))
            if clone_dir:
                shutil.rmtree(clone_dir, ignore_errors=True)
