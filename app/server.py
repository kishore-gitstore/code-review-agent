"""
app/server.py — FastAPI application: webhook receiver + manual trigger endpoint.

Supports multiple concurrent MR reviews. Each review runs as a background task
so webhooks return 202 immediately (GitLab's 10-second timeout is never hit).
Duplicate reviews for the same MR are suppressed while one is already running.
Progress is queryable at GET /review/status/{project_id}/{mr_iid}.
"""

from __future__ import annotations
import structlog
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional
from fastapi import FastAPI, Request, HTTPException, Header, BackgroundTasks
from fastapi.responses import JSONResponse

from app.settings import get_settings
from app.llm.factory import get_llm_client
from app.gitlab.client import get_gitlab_client
from app.agents.coordinator import CoordinatorAgent
from app.utils.models import MRContext
from app.utils.diff_parser import get_changed_files, get_path_map

log = structlog.get_logger(__name__)

# ── In-memory review progress tracker ────────────────────────────────────────

@dataclass
class ReviewProgress:
    project_id:     str | int
    mr_iid:         int
    mr_title:       str = ""
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
            "project_id":     self.project_id,
            "mr_iid":         self.mr_iid,
            "mr_title":       self.mr_title,
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


def _rkey(project_id: str | int, mr_iid: int) -> str:
    return f"{project_id}/{mr_iid}"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _register(project_id: str | int, mr_iid: int, mr_title: str, agent_names: list[str]) -> bool:
    """Register a new review. Returns False if one is already running for this MR."""
    key = _rkey(project_id, mr_iid)
    existing = _reviews.get(key)
    if existing and existing.status == "running":
        log.warning("review.duplicate_skipped", mr=mr_iid, project=project_id)
        return False
    _reviews[key] = ReviewProgress(
        project_id=project_id,
        mr_iid=mr_iid,
        mr_title=mr_title,
        status="running",
        started_at=_now(),
        agents_all=list(agent_names),
    )
    log.info("review.registered", mr=mr_iid, project=project_id, agents=agent_names)
    return True


def _on_agent_start(project_id: str | int, mr_iid: int, agent: str) -> None:
    if p := _reviews.get(_rkey(project_id, mr_iid)):
        if agent not in p.agents_running:
            p.agents_running.append(agent)
        log.info("review.agent.started", mr=mr_iid, agent=agent,
                 progress=f"{len(p.agents_done)}/{len(p.agents_all)}")


def _on_agent_done(project_id: str | int, mr_iid: int, agent: str, findings: int) -> None:
    if p := _reviews.get(_rkey(project_id, mr_iid)):
        if agent in p.agents_running:
            p.agents_running.remove(agent)
        if agent not in p.agents_done:
            p.agents_done.append(agent)
        log.info("review.agent.done", mr=mr_iid, agent=agent, findings=findings,
                 progress=f"{len(p.agents_done)}/{len(p.agents_all)}",
                 remaining=p.agents_pending)


def _finish(project_id: str | int, mr_iid: int, total: int, critical: int, high: int) -> None:
    if p := _reviews.get(_rkey(project_id, mr_iid)):
        p.status = "done"
        p.finished_at = _now()
        p.total_issues = total
        p.critical_count = critical
        p.high_count = high
        p.agents_running = []
        log.info("review.done", mr=mr_iid, total=total, critical=critical, high=high)
    _trim()


def _fail(project_id: str | int, mr_iid: int, error: str) -> None:
    if p := _reviews.get(_rkey(project_id, mr_iid)):
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
    cfg = get_settings()
    log.info("startup", model=cfg.copilot_model, gitlab=cfg.gitlab_url)
    health = await get_llm_client().health_check()
    if health["status"] != "ok":
        log.warning("copilot.unavailable", detail=health)
    else:
        log.info("copilot.ready", model=health["model"])
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
        copilot = await get_llm_client().health_check()
        return {"status": copilot["status"], "provider": copilot.get("provider"),
                "model": copilot.get("model")}

    # ── Progress endpoints ────────────────────────────────────────────────
    @app.get("/review/active")
    async def list_reviews():
        """List all running and recently completed reviews."""
        return [p.as_dict() for p in _reviews.values()]

    @app.get("/review/status/{project_id}/{mr_iid}")
    async def get_review_status(project_id: str, mr_iid: int):
        """Get live progress for a specific MR review."""
        key = _rkey(project_id, mr_iid)
        if key not in _reviews:
            raise HTTPException(status_code=404, detail="No review found for this MR")
        return _reviews[key].as_dict()

    # ── GitLab Webhook ────────────────────────────────────────────────────
    @app.post("/webhook/gitlab", status_code=202)
    async def gitlab_webhook(
        request: Request,
        background_tasks: BackgroundTasks,
        x_gitlab_token: str | None = Header(default=None),
        x_gitlab_event: str | None = Header(default=None),
    ):
        await request.body()  # consume body before json()

        if not get_gitlab_client().verify_webhook(x_gitlab_token or ""):
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

        log.info("webhook.mr_event", project=project_id, mr=mr_iid, action=action)

        # Suppress duplicate if already running
        existing = _reviews.get(_rkey(project_id, mr_iid))
        if existing and existing.status == "running":
            log.warning("webhook.duplicate_ignored", mr=mr_iid, project=project_id)
            return JSONResponse({
                "status": "already_in_progress",
                "mr_iid": mr_iid,
                "progress": existing.as_dict(),
            })

        background_tasks.add_task(_run_review_bg, project_id, mr_iid, mr_title)
        return JSONResponse({
            "status": "accepted",
            "mr_iid": mr_iid,
            "status_url": f"/review/status/{project_id}/{mr_iid}",
        }, status_code=202)

    # ── Manual trigger ────────────────────────────────────────────────────
    @app.post("/review", status_code=202)
    async def manual_review(body: dict, background_tasks: BackgroundTasks):
        """
        Manually trigger a review.
        POST /review  { "project_id": "group/repo", "mr_iid": 42 }
        Returns 202 immediately; poll GET /review/status/{project_id}/{mr_iid} for progress.
        """
        project_id = body.get("project_id")
        mr_iid     = body.get("mr_iid")
        if not project_id or not mr_iid:
            raise HTTPException(status_code=400, detail="project_id and mr_iid required")

        mr_iid = int(mr_iid)
        existing = _reviews.get(_rkey(project_id, mr_iid))
        if existing and existing.status == "running":
            return JSONResponse({
                "status": "already_in_progress",
                "mr_iid": mr_iid,
                "progress": existing.as_dict(),
            })

        background_tasks.add_task(_run_review_bg, project_id, mr_iid, "")
        return JSONResponse({
            "status": "accepted",
            "mr_iid": mr_iid,
            "status_url": f"/review/status/{project_id}/{mr_iid}",
        }, status_code=202)

    return app


# ── Background review runner ──────────────────────────────────────────────────

async def _run_review_bg(project_id: str | int, mr_iid: int, mr_title: str) -> None:
    """Background entry-point — wraps _run_review with error handling."""
    try:
        await _run_review(project_id, mr_iid, mr_title)
    except Exception as exc:
        log.error("review.failed", mr=mr_iid, project=project_id, error=str(exc))
        _fail(project_id, mr_iid, str(exc))


async def _run_review(project_id: str | int, mr_iid: int, mr_title: str = "") -> None:
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

    if not _register(project_id, mr_iid, mr_title, agent_names):
        return  # duplicate — already running

    gitlab     = get_gitlab_client()
    mr_details = await gitlab.get_mr_details(project_id, mr_iid)
    diff       = await gitlab.get_mr_diff(project_id, mr_iid)
    mr_title   = mr_title or mr_details.get("title", "")

    if p := _reviews.get(_rkey(project_id, mr_iid)):
        p.mr_title = mr_title

    ctx = MRContext(
        project_id=project_id,
        mr_iid=mr_iid,
        title=mr_title,
        description=mr_details.get("description", "") or "",
        author=mr_details.get("author", {}).get("username", "unknown"),
        source_branch=mr_details.get("source_branch", ""),
        target_branch=mr_details.get("target_branch", ""),
        diff=diff,
        changed_files=get_changed_files(diff),
    )

    result = await coordinator.run(
        ctx,
        on_agent_start=lambda agent: _on_agent_start(project_id, mr_iid, agent),
        on_agent_done=lambda agent, n: _on_agent_done(project_id, mr_iid, agent, n),
    )
    await gitlab.post_review(result, mr_details, get_path_map(diff))
    _finish(project_id, mr_iid, result.total_issues, result.critical_count, result.high_count)
