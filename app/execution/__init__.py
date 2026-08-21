"""
app/execution — Hardware detection + CPU/GPU execution-mode policy.

See hardware.py (detect_hardware) and policy.py (resolve_execution_policy /
get_execution_policy) for the two halves of this: what hardware exists, and
what that implies for concurrency across reviews, analyzers, and LLM requests.
"""

from .hardware import HardwareInfo, detect_hardware
from .policy import (
    ExecutionPolicy,
    get_execution_policy,
    reset_execution_policy,
    resolve_execution_policy,
    set_execution_policy,
)

__all__ = [
    "HardwareInfo",
    "detect_hardware",
    "ExecutionPolicy",
    "resolve_execution_policy",
    "get_execution_policy",
    "set_execution_policy",
    "reset_execution_policy",
]
