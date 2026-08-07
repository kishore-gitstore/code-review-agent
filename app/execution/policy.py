"""
app/execution/policy.py — Resolves EXECUTION_MODE (auto/cpu/gpu) into a
concrete ExecutionPolicy the rest of the app reads to size its concurrency:
StaticAnalysisRunner (max_parallel_analyzers), OllamaClient + CoordinatorAgent
(max_parallel_llm_requests), and server.py (max_parallel_reviews).

`auto` picks gpu when detect_hardware() found one, else cpu. Resolved once at
startup (app/server.py's lifespan) via resolve_execution_policy() +
set_execution_policy(); get_execution_policy() returns a safe cpu-mode
default if nothing has resolved yet, so a component built outside the normal
startup path (a unit test constructing OllamaClient() directly) doesn't need
an explicit "has this run yet" check before reading it.
"""

from __future__ import annotations
import structlog
from dataclasses import dataclass
from typing import Literal

from .hardware import HardwareInfo

log = structlog.get_logger(__name__)

Mode = Literal["cpu", "gpu"]


@dataclass(frozen=True, slots=True)
class ExecutionPolicy:
    mode: Mode
    max_parallel_reviews: int
    max_parallel_analyzers: int
    max_parallel_llm_requests: int


_DEFAULT_POLICY = ExecutionPolicy(
    mode="cpu", max_parallel_reviews=1, max_parallel_analyzers=1, max_parallel_llm_requests=1,
)

_policy: ExecutionPolicy = _DEFAULT_POLICY


def resolve_execution_policy(cfg, hardware: HardwareInfo) -> ExecutionPolicy:
    requested = cfg.execution_mode.lower().strip()
    if requested == "auto":
        mode: Mode = "gpu" if hardware.gpu_available else "cpu"
    elif requested in ("cpu", "gpu"):
        mode = requested
    else:
        log.warning("execution.mode_invalid", requested=requested, fallback="cpu")
        mode = "cpu"

    if mode == "cpu":
        # Hard requirement, not just "up to configured max" — a CPU-only box
        # can't truly run any of these concurrently; force 1 regardless of
        # MAX_PARALLEL_* to avoid self-inflicted contention/timeouts.
        policy = ExecutionPolicy(
            mode="cpu", max_parallel_reviews=1, max_parallel_analyzers=1, max_parallel_llm_requests=1,
        )
    else:
        policy = ExecutionPolicy(
            mode="gpu",
            max_parallel_reviews=max(1, cfg.max_parallel_reviews),
            max_parallel_analyzers=max(1, cfg.max_parallel_analyzers),
            max_parallel_llm_requests=max(1, cfg.max_parallel_llm_requests),
        )

    log.info(
        "execution.mode_resolved", requested=requested, resolved=policy.mode,
        max_parallel_reviews=policy.max_parallel_reviews,
        max_parallel_analyzers=policy.max_parallel_analyzers,
        max_parallel_llm_requests=policy.max_parallel_llm_requests,
        gpu_available=hardware.gpu_available, gpu_name=hardware.gpu_name,
    )
    return policy


def set_execution_policy(policy: ExecutionPolicy) -> None:
    global _policy
    _policy = policy


def get_execution_policy() -> ExecutionPolicy:
    return _policy


def reset_execution_policy() -> None:
    """Restore the safe cpu-mode default — useful for test isolation."""
    global _policy
    _policy = _DEFAULT_POLICY
