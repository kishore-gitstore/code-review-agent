"""
app/execution/hardware.py — Detects CPU/RAM/GPU on the machine this process
is running on, plus whether the configured Ollama server is actually using
GPU acceleration for the active model. Feeds EXECUTION_MODE=auto (see
policy.py), which needs to know whether GPU capacity actually exists before
picking gpu-mode concurrency.
"""

from __future__ import annotations
import os
import platform
import structlog
from dataclasses import dataclass
from typing import Optional

import httpx

from app.utils.process_runner import ProcessNotFound, run_process

log = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class HardwareInfo:
    cpu_count: int
    total_ram_gb: Optional[float]
    gpu_available: bool
    gpu_name: Optional[str]
    gpu_memory_mb: Optional[int]
    ollama_using_gpu: Optional[bool]  # None = couldn't be determined (unreachable, no model loaded yet)


async def detect_hardware(*, ollama_base_url: str | None = None, ollama_model: str | None = None) -> HardwareInfo:
    cpu_count = os.cpu_count() or 1
    total_ram_gb = _detect_ram_gb()
    gpu_available, gpu_name, gpu_memory_mb = await _detect_gpu()
    ollama_using_gpu = await _detect_ollama_gpu_usage(ollama_base_url, ollama_model) if ollama_base_url else None

    info = HardwareInfo(
        cpu_count=cpu_count,
        total_ram_gb=total_ram_gb,
        gpu_available=gpu_available,
        gpu_name=gpu_name,
        gpu_memory_mb=gpu_memory_mb,
        ollama_using_gpu=ollama_using_gpu,
    )
    log.info(
        "hardware.detected",
        cpu_count=info.cpu_count,
        total_ram_gb=info.total_ram_gb,
        gpu_available=info.gpu_available,
        gpu_name=info.gpu_name,
        gpu_memory_mb=info.gpu_memory_mb,
        ollama_using_gpu=info.ollama_using_gpu,
    )
    return info


def _detect_ram_gb() -> Optional[float]:
    """Linux only (this project targets Ubuntu) — reads /proc/meminfo. Returns
    None on platforms without it (e.g. local Windows/macOS dev) rather than
    raising; RAM info is advisory, not required for anything to function."""
    if platform.system() != "Linux":
        return None
    try:
        with open("/proc/meminfo", encoding="utf-8") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    kb = int(line.split()[1])
                    return round(kb / (1024 * 1024), 1)
    except (OSError, ValueError, IndexError) as exc:
        log.warning("hardware.ram_detect_failed", error=str(exc))
    return None


async def _detect_gpu() -> tuple[bool, Optional[str], Optional[int]]:
    """NVIDIA only, via nvidia-smi — the common case for self-hosted Ollama.
    No nvidia-smi on PATH simply means no supported GPU; not an error."""
    try:
        result = await run_process(
            ["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"],
            timeout=10, tool_name="nvidia-smi",
        )
    except ProcessNotFound:
        return False, None, None

    if result.returncode != 0 or not result.stdout.strip():
        return False, None, None

    first_line = result.stdout.strip().splitlines()[0]
    parts = [p.strip() for p in first_line.split(",")]
    name = parts[0] if parts else None
    try:
        memory_mb = int(parts[1]) if len(parts) > 1 else None
    except ValueError:
        memory_mb = None
    return True, name, memory_mb


async def _detect_ollama_gpu_usage(base_url: str, model: str | None) -> Optional[bool]:
    """Ollama's /api/ps lists currently loaded models with size_vram — a
    non-zero value means that model is (at least partly) resident on GPU.
    Returns None if Ollama isn't reachable or no model is loaded yet (nothing
    to measure yet, not "not using GPU")."""
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            r = await client.get(f"{base_url.rstrip('/')}/api/ps")
        r.raise_for_status()
        loaded = r.json().get("models", [])
    except Exception as exc:
        log.debug("hardware.ollama_gpu_check_failed", error=str(exc))
        return None

    if not loaded:
        return None

    candidates = [m for m in loaded if not model or model in m.get("name", "")] or loaded
    return any(m.get("size_vram", 0) > 0 for m in candidates)
