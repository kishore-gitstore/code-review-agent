"""
app/reporting.py — Shared execution-report collector for CoordinatorAgent runs.

Wires into CoordinatorAgent.run()'s on_llm_request/on_analyzer_done/on_phase
callbacks, then renders the same data three ways: a console table, a
structlog entry, and a JSON file under reports/. Used by both the live
GitLab webhook flow (server.py) and the local CLI (tests/review_local.py) so
the two can't drift out of sync, and read back by server.py's /dashboard.
"""

from __future__ import annotations
import json
import sys
import structlog
from datetime import datetime
from pathlib import Path

from app.execution.policy import get_execution_policy
from app.llm.factory import get_active_model_label

log = structlog.get_logger(__name__)

REPORTS_DIR = Path(__file__).resolve().parent.parent / "reports"

# ── colored console output (auto-disabled when stdout isn't a TTY, e.g.
# piped into a file or captured by a supervisor) ──────────────────────────
_COLOR = sys.stdout.isatty()
_ANSI = {"red": "31", "green": "32", "yellow": "33", "bold": "1"}


def _c(text: str, color: str) -> str:
    return f"\033[{_ANSI[color]}m{text}\033[0m" if _COLOR else text


def _fmt_duration(seconds: float) -> str:
    """HH:MM:SS.mmm — millisecond precision."""
    total_ms = max(0, int(round(seconds * 1000)))
    hours, rem_ms = divmod(total_ms, 3_600_000)
    minutes, rem_ms = divmod(rem_ms, 60_000)
    secs, ms = divmod(rem_ms, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}.{ms:03d}"


def _print_table(headers: tuple, rows: list[tuple], status_col: int | None = None) -> None:
    widths = [
        max(len(h), *(len(r[i]) for r in rows)) if rows else len(h)
        for i, h in enumerate(headers)
    ]
    status_color = {"Success": "green", "Failed": "red", "Cancelled": "yellow"}

    def fmt(row, colorize=False):
        cells = []
        for i, cell in enumerate(row):
            padded = cell.ljust(widths[i])
            if colorize and i == status_col and cell in status_color:
                padded = _c(padded, status_color[cell])
            cells.append(padded)
        return " | ".join(cells)

    print(fmt(headers))
    print("-+-".join("-" * w for w in widths))
    if not rows:
        print("(none)")
    for row in rows:
        print(fmt(row, colorize=True))


class ExecutionReport:
    """
    One review's execution stats, collected via callbacks passed to
    CoordinatorAgent.run(), then rendered as a console table
    (print_console), a structlog entry (log_summary), and a JSON file
    (save_json) for the dashboard to read back.

    project_id/mr_iid/mr_title are None for local CLI reviews (no MR to
    attach to) and set for live GitLab-triggered reviews.
    """

    def __init__(
        self,
        repository: str,
        branch: str,
        instance: str = "",
        project_id: str | int | None = None,
        project_name: str = "",
        mr_iid: int | None = None,
        mr_title: str = "",
        mr_author: str = "",
        mr_url: str = "",
    ):
        self.repository = repository
        self.branch = branch
        self.instance = instance
        self.project_id = project_id
        self.project_name = project_name
        self.mr_iid = mr_iid
        self.mr_title = mr_title
        self.mr_author = mr_author
        self.mr_url = mr_url
        self.model = get_active_model_label()
        self.execution_mode = get_execution_policy().mode

        self.llm_requests: list[dict] = []  # agent, file, chunk, model, start, end, duration_s, status
        self.analyzers:    list[dict] = []  # tool, language, files, findings, duration_ms
        self.phase_ms:     dict[str, int] = {}

        self.files_changed = 0
        self.lines_added   = 0
        self.lines_removed = 0

        self.started_at:  datetime | None = None
        self.finished_at: datetime | None = None
        self.status = "running"  # see the status property below
        self.error = None
        self.result = None  # ReviewResult, once available

    # ── callbacks wired into CoordinatorAgent.run() ──────────────────────────

    def on_llm_request(self, agent, file_path, started, finished, success) -> None:
        self.llm_requests.append({
            "agent": agent, "file": file_path, "chunk": 1, "model": self.model,
            "start": started, "end": finished,
            "duration_s": (finished - started).total_seconds(),
            "status": "Success" if success else "Failed",
        })

    def on_analyzer_done(self, tool, language, files_count, findings_count, duration_ms) -> None:
        self.analyzers.append({
            "tool": tool, "language": language, "files": files_count,
            "findings": findings_count, "duration_ms": duration_ms,
        })

    def on_phase(self, name, duration_ms) -> None:
        self.phase_ms[name] = duration_ms

    # ── derived stats ─────────────────────────────────────────────────────

    @property
    def total_duration_s(self) -> float:
        if not (self.started_at and self.finished_at):
            return 0.0
        return (self.finished_at - self.started_at).total_seconds()

    @property
    def llm_durations(self) -> list[float]:
        return [r["duration_s"] for r in self.llm_requests]

    @property
    def llm_requests_failed(self) -> int:
        return sum(1 for r in self.llm_requests if r["status"] == "Failed")

    @property
    def status(self) -> str:
        """
        running | success | partial | failed | cancelled.

        "partial" is derived, never assigned: the review ran to completion (so
        the caller sets "success") but at least one LLM request inside it
        failed. Those failures are swallowed per-agent by design — a broken
        agent returns no findings instead of sinking the review — so a review
        against a dead LLM used to be reported as a clean "success" with zero
        issues. Deriving it here makes every surface that reads .status (the
        console footer, the structlog line, the JSON the dashboard reads,
        review_local.py's exit code) degrade together, with no call site able
        to forget.
        """
        if self._status == "success" and self.llm_requests_failed:
            return "partial"
        return self._status

    @status.setter
    def status(self, value: str) -> None:
        self._status = value

    @property
    def error(self) -> str | None:
        """The explicit error if one was set, else a synthesized one for
        "partial" — nothing raised on that path, so no caller sets .error."""
        if self._error is None and self.status == "partial":
            return (f"{self.llm_requests_failed} of {len(self.llm_requests)} "
                    f"LLM request(s) failed — findings are incomplete")
        return self._error

    @error.setter
    def error(self, value: str | None) -> None:
        self._error = value

    def summary_dict(self) -> dict:
        durations = self.llm_durations
        result = self.result
        return {
            "repository": self.repository,
            "branch": self.branch,
            "instance": self.instance,
            "project_id": self.project_id,
            "project_name": self.project_name,
            "mr_iid": self.mr_iid,
            "mr_title": self.mr_title,
            "mr_author": self.mr_author,
            "mr_url": self.mr_url,
            "model": self.model,
            "execution_mode": self.execution_mode,
            "status": self.status,
            "error": self.error,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "total_duration_s": self.total_duration_s,
            "phase_ms": self.phase_ms,
            "files_changed": self.files_changed,
            "lines_added": self.lines_added,
            "lines_removed": self.lines_removed,
            "static_analyzers_run": len(self.analyzers),
            "llm_requests_total": len(self.llm_requests),
            "llm_requests_failed": self.llm_requests_failed,
            "llm_avg_s":     sum(durations) / len(durations) if durations else 0.0,
            "llm_fastest_s": min(durations) if durations else 0.0,
            "llm_slowest_s": max(durations) if durations else 0.0,
            "total_issues":    result.total_issues if result else 0,
            "critical_count":  result.critical_count if result else 0,
            "high_count":      result.high_count if result else 0,
            "medium_count":    result.medium_count if result else 0,
            "low_count":       result.low_count if result else 0,
            "ai_suggestions":  sum(1 for c in (result.comments if result else []) if c.suggestion),
            "ai_fixes":        result.fix_count if result else 0,
            "summary":         result.summary if result else "",
            "static_analysis_notes": list(result.static_analysis_notes) if result else [],
            "agent_stats":     self.agent_stats(),
            "findings_by_agent": _count_by(result.comments if result else [], lambda c: c.agent),
            "findings_by_file":  _count_by(result.comments if result else [], lambda c: c.file_path),
            # The findings themselves (title/description/suggestion/fix), so the
            # dashboard can show what was actually reported — not just counts.
            "comments": [c.model_dump(mode="json") for c in (result.comments if result else [])],
            "llm_requests": [
                {**r, "start": r["start"].isoformat(), "end": r["end"].isoformat()}
                for r in self.llm_requests
            ],
            "static_analysis": self.analyzers,
        }

    def agent_stats(self) -> dict[str, dict]:
        """
        Per-agent LLM roll-up: request count, failures, total seconds. Small
        enough to ship in the slim report list (see list_reports) so the
        dashboard can chart agent latency/failure rate across many reviews
        without downloading every per-request table.
        """
        stats: dict[str, dict] = {}
        for r in self.llm_requests:
            agg = stats.setdefault(r["agent"], {"requests": 0, "failed": 0, "total_s": 0.0})
            agg["requests"] += 1
            agg["total_s"]  += r["duration_s"]
            if r["status"] == "Failed":
                agg["failed"] += 1
        return stats

    # ── rendering ─────────────────────────────────────────────────────────

    def print_console(self) -> None:
        result = self.result
        width  = 90
        rule   = "=" * width

        print("\n" + rule)
        print(_c("Code Review Agent — Execution Summary".center(width), "bold"))
        print(rule + "\n")

        analyzer_names = sorted({a["tool"] for a in self.analyzers})
        if self.instance:
            print(f"GitLab Instance      : {self.instance}")
        if self.project_id is not None:
            print(f"Project ID           : {self.project_id}")
        if self.project_name:
            print(f"Project Name         : {self.project_name}")
        if self.mr_iid is not None:
            mr_line = f"!{self.mr_iid}" + (f" — {self.mr_title}" if self.mr_title else "")
            print(f"Merge Request        : {mr_line}")
        if self.mr_author:
            print(f"Author               : {self.mr_author}")
        print(f"Repository           : {self.repository}")
        print(f"Branch               : {self.branch}")
        print(f"Model                : {self.model}")
        print(f"Execution Mode       : {self.execution_mode.upper()}")
        print(f"Static Analyzers     : {', '.join(analyzer_names) or 'none'}")
        print(f"Files Changed        : {self.files_changed}")
        print(f"Lines Reviewed       : +{self.lines_added} / -{self.lines_removed}")
        print()

        if self.started_at:
            print(f"Review Started       : {self.started_at.strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]}")
        if self.finished_at:
            print(f"Review Finished      : {self.finished_at.strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]}")
        print(f"Total Duration       : {_fmt_duration(self.total_duration_s)}")
        print()

        if "static_analysis" in self.phase_ms:
            print(f"Static Analysis Time : {_fmt_duration(self.phase_ms['static_analysis'] / 1000)}")
        if "llm_review" in self.phase_ms:
            print(f"LLM Review Time      : {_fmt_duration(self.phase_ms['llm_review'] / 1000)}")
        if "merge" in self.phase_ms:
            print(f"Merge Time           : {_fmt_duration(self.phase_ms['merge'] / 1000)}")
        summary_row = next((r for r in self.llm_requests if r["agent"] == "summary"), None)
        if summary_row:
            print(f"Summary Generation   : {_fmt_duration(summary_row['duration_s'])}")
        print()

        if result:
            print(f"Total Issues         : {result.total_issues}")
            for label, count, color in (
                ("Critical", result.critical_count, "red"),
                ("High",     result.high_count,     "red"),
                ("Medium",   result.medium_count,   "yellow"),
                ("Low",      result.low_count,       "green"),
            ):
                text = f"  {label:<10}: {count}"
                print(_c(text, color) if count > 0 else text)
            ai_suggestions = sum(1 for c in result.comments if c.suggestion)
            print(f"AI Suggestions       : {ai_suggestions}")
            print(f"AI Fixes (applicable): {result.fix_count}")
        print()

        print(rule)
        print(_c("LLM Request Statistics".center(width), "bold"))
        print(rule + "\n")
        self._print_llm_table()

        durations = self.llm_durations
        if durations:
            print(f"\nAverage LLM Time     : {sum(durations) / len(durations):.2f}s")
            print(f"Fastest Request      : {min(durations):.2f}s")
            print(f"Slowest Request      : {max(durations):.2f}s")
        failed = self.llm_requests_failed
        print(f"Total LLM Requests   : {len(self.llm_requests)}")
        print(f"Successful Requests  : {len(self.llm_requests) - failed}")
        print(f"Failed Requests      : {failed}")

        if self.analyzers:
            print("\n" + rule)
            print(_c("Static Analyzer Execution".center(width), "bold"))
            print(rule + "\n")
            self._print_analyzer_table()

        print("\n" + rule)
        if self.status == "success":
            print(_c(f"Review completed successfully in {_fmt_duration(self.total_duration_s)}", "green"))
        elif self.status == "partial":
            print(_c(f"Review INCOMPLETE after {_fmt_duration(self.total_duration_s)}: "
                     f"{self.error}", "yellow"))
        elif self.status == "cancelled":
            print(_c(f"Review cancelled after {_fmt_duration(self.total_duration_s)}", "yellow"))
        else:
            print(_c(f"Review failed after {_fmt_duration(self.total_duration_s)}: {self.error}", "red"))
        print(rule)

        # print() alone isn't enough here: under uvicorn --reload the worker's
        # stdout is a pipe, not a tty, so CPython block-buffers it — this
        # multi-KB report can sit unflushed for a long time. structlog's
        # PrintLogger (used by log_summary(), called right after this) flushes
        # on every call, which is exactly why that single log line was showing
        # up while this whole table silently didn't.
        sys.stdout.flush()

    def _print_llm_table(self) -> None:
        # Sort by start time, not insertion order — in GPU/parallel mode
        # (MAX_PARALLEL_LLM_REQUESTS > 1) requests overlap and can finish out
        # of order. Each row's own duration is already correct wall time for
        # that single request regardless of concurrency; it's only the phase
        # totals above (LLM Review Time) that must use span-of-wall-clock
        # rather than sum-of-durations to avoid overcounting overlap — which
        # is exactly what coordinator.py's on_phase timer already measures.
        ordered = sorted(self.llm_requests, key=lambda r: r["start"])
        headers = ("#", "Agent", "File", "Chunk", "Model", "Start", "End", "Duration", "Status")
        rows = [
            (
                str(i), r["agent"], r["file"], str(r["chunk"]), r["model"],
                r["start"].strftime("%H:%M:%S"), r["end"].strftime("%H:%M:%S"),
                f"{r['duration_s']:.2f}s", r["status"],
            )
            for i, r in enumerate(ordered, start=1)
        ]
        _print_table(headers, rows, status_col=8)

    def _print_analyzer_table(self) -> None:
        headers = ("Analyzer", "Language", "Files", "Issues", "Time")
        rows = [
            (a["tool"], a["language"], str(a["files"]), str(a["findings"]), f"{a['duration_ms'] / 1000:.2f}s")
            for a in self.analyzers
        ]
        _print_table(headers, rows)

    def save_json(self) -> Path:
        REPORTS_DIR.mkdir(exist_ok=True)
        stamp = self.started_at.strftime("%Y%m%d_%H%M%S") if self.started_at else "unknown"
        if self.project_id is not None and self.mr_iid is not None:
            safe_project = str(self.project_id).replace("/", "_")
            safe_instance = f"{self.instance}_" if self.instance else ""
            name = f"review_{safe_instance}{safe_project}_{self.mr_iid}_{stamp}.json"
        else:
            name = f"review_local_{stamp}.json"
        path = REPORTS_DIR / name
        # Explicit encoding, not the process locale: json.dumps escapes
        # non-ASCII by default today, but the summary is full of emoji and
        # nothing should make persisting a report depend on the code page.
        path.write_text(json.dumps(self.summary_dict(), indent=2), encoding="utf-8")
        return path

    def log_summary(self) -> None:
        log.info("review.execution_summary", **self.summary_dict())

    def persist_and_render(self) -> Path | None:
        """
        Write the report, then display it — in that order, and independently.

        The dashboard has no source for a finished review other than this JSON,
        and displaying it is the risky half: print_console/log_summary push the
        summary's emoji verdict at stdout, which raised UnicodeEncodeError
        under the Windows service (a cp1252 log file) and took the not-yet-run
        save_json() down with it, so completed reviews left no trace at all.
        force_utf8_streams() fixes that stdout; this ordering makes the record
        independent of the table that describes it either way.

        Returns the path written, or None if even that failed.
        """
        path = None
        try:
            path = self.save_json()
        except Exception as exc:
            log.error("report.save_failed", mr=self.mr_iid, error=str(exc))
        try:
            self.print_console()
            self.log_summary()
        except Exception as exc:
            log.warning("report.render_failed", mr=self.mr_iid, error=str(exc))
        return path


# ── dashboard read-side ────────────────────────────────────────────────────

# Per-item arrays that can run to hundreds of entries per report. The list
# view only needs the roll-ups (counts, agent_stats, phase_ms); the dashboard
# fetches these via read_report() when a single report is opened.
_HEAVY_FIELDS = ("llm_requests", "comments")


def _count_by(comments, key) -> dict[str, int]:
    """{key(comment): count} roll-up — shipped in the slim report list so the
    dashboard can chart findings per agent and hotspot files across reviews
    without downloading every report's full comments array."""
    counts: dict[str, int] = {}
    for c in comments:
        k = key(c)
        counts[k] = counts.get(k, 0) + 1
    return counts


def list_reports(limit: int = 200, slim: bool = True) -> list[dict]:
    """Summary dicts for the most recently finished reviews, newest first —
    backs the /api/reports endpoint the dashboard page polls. With slim=True
    (the default) the heavy per-request/per-finding arrays are dropped."""
    if not REPORTS_DIR.exists():
        return []
    files = sorted(REPORTS_DIR.glob("review_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    reports = []
    for f in files[:limit]:
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if slim:
            for key in _HEAVY_FIELDS:
                data.pop(key, None)
        data["_file"] = f.name
        reports.append(data)
    return reports


def read_report(filename: str) -> dict:
    """
    Read one saved report by filename. filename comes straight from an HTTP
    path parameter in server.py's dashboard API, so this guards against path
    traversal by requiring the resolved path to (a) stay inside REPORTS_DIR
    and (b) match the exact naming scheme save_json() produces — rejecting
    anything else (e.g. '../../etc/passwd') before ever touching the filesystem.
    """
    if not filename.startswith("review_") or not filename.endswith(".json") or "/" in filename or "\\" in filename:
        raise ValueError(f"invalid report filename: {filename!r}")
    path = (REPORTS_DIR / filename).resolve()
    if path.parent != REPORTS_DIR.resolve():
        raise ValueError(f"invalid report filename: {filename!r}")
    if not path.is_file():
        raise FileNotFoundError(filename)
    return json.loads(path.read_text(encoding="utf-8"))
