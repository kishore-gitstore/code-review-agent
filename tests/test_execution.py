"""
tests/test_execution.py — Hardware detection + CPU/GPU execution-mode policy.
"""

import pytest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.execution.hardware import HardwareInfo, detect_hardware
from app.execution.policy import (
    ExecutionPolicy,
    get_execution_policy,
    reset_execution_policy,
    resolve_execution_policy,
    set_execution_policy,
)
from app.utils.process_runner import ProcessNotFound, ProcessResult


def _cfg(**overrides):
    base = dict(execution_mode="auto", max_parallel_reviews=1,
                max_parallel_analyzers=1, max_parallel_llm_requests=1)
    base.update(overrides)
    return SimpleNamespace(**base)


# ── Hardware detection ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_detect_hardware_reports_no_gpu_when_nvidia_smi_missing():
    with patch("app.execution.hardware.run_process", new=AsyncMock(side_effect=ProcessNotFound("no nvidia-smi"))):
        info = await detect_hardware()
    assert info.gpu_available is False
    assert info.gpu_name is None
    assert info.cpu_count >= 1


@pytest.mark.asyncio
async def test_detect_hardware_parses_nvidia_smi_output():
    fake_result = ProcessResult(returncode=0, stdout="NVIDIA A100, 40960\n", stderr="", timed_out=False, duration_ms=5)
    with patch("app.execution.hardware.run_process", new=AsyncMock(return_value=fake_result)):
        info = await detect_hardware()
    assert info.gpu_available is True
    assert info.gpu_name == "NVIDIA A100"
    assert info.gpu_memory_mb == 40960


@pytest.mark.asyncio
async def test_detect_hardware_ollama_gpu_check_returns_none_when_unreachable():
    with patch("app.execution.hardware.run_process", new=AsyncMock(side_effect=ProcessNotFound("no nvidia-smi"))), \
         patch("app.execution.hardware.httpx.AsyncClient") as mock_client:
        mock_client.side_effect = Exception("connection refused")
        info = await detect_hardware(ollama_base_url="http://localhost:11434", ollama_model="qwen3-coder")
    assert info.ollama_using_gpu is None


# ── Execution policy resolution ────────────────────────────────────────────────

def test_resolve_auto_picks_gpu_when_hardware_available():
    hardware = HardwareInfo(cpu_count=8, total_ram_gb=32.0, gpu_available=True,
                             gpu_name="NVIDIA A100", gpu_memory_mb=40960, ollama_using_gpu=True)
    policy = resolve_execution_policy(_cfg(execution_mode="auto", max_parallel_llm_requests=4), hardware)
    assert policy.mode == "gpu"
    assert policy.max_parallel_llm_requests == 4


def test_resolve_auto_picks_cpu_when_no_gpu():
    hardware = HardwareInfo(cpu_count=4, total_ram_gb=16.0, gpu_available=False,
                             gpu_name=None, gpu_memory_mb=None, ollama_using_gpu=None)
    policy = resolve_execution_policy(_cfg(execution_mode="auto", max_parallel_llm_requests=4), hardware)
    assert policy.mode == "cpu"


def test_resolve_cpu_forces_everything_to_one_regardless_of_configured_max():
    """CPU mode is a hard requirement, not just a default — a CPU-only box
    can't truly parallelise, so even an explicit MAX_PARALLEL_*=8 must not
    override it (that would reintroduce the self-inflicted-timeout bug)."""
    hardware = HardwareInfo(cpu_count=4, total_ram_gb=16.0, gpu_available=True,
                             gpu_name="some-gpu", gpu_memory_mb=8192, ollama_using_gpu=None)
    policy = resolve_execution_policy(
        _cfg(execution_mode="cpu", max_parallel_reviews=8, max_parallel_analyzers=8, max_parallel_llm_requests=8),
        hardware,
    )
    assert policy.mode == "cpu"
    assert policy.max_parallel_reviews == 1
    assert policy.max_parallel_analyzers == 1
    assert policy.max_parallel_llm_requests == 1


def test_resolve_gpu_uses_configured_max_parallel_values():
    hardware = HardwareInfo(cpu_count=16, total_ram_gb=64.0, gpu_available=True,
                             gpu_name="NVIDIA A100", gpu_memory_mb=40960, ollama_using_gpu=True)
    policy = resolve_execution_policy(
        _cfg(execution_mode="gpu", max_parallel_reviews=3, max_parallel_analyzers=5, max_parallel_llm_requests=2),
        hardware,
    )
    assert policy.mode == "gpu"
    assert policy.max_parallel_reviews == 3
    assert policy.max_parallel_analyzers == 5
    assert policy.max_parallel_llm_requests == 2


def test_resolve_invalid_mode_falls_back_to_cpu():
    hardware = HardwareInfo(cpu_count=4, total_ram_gb=None, gpu_available=False,
                             gpu_name=None, gpu_memory_mb=None, ollama_using_gpu=None)
    policy = resolve_execution_policy(_cfg(execution_mode="quantum"), hardware)
    assert policy.mode == "cpu"


def test_get_execution_policy_defaults_to_safe_cpu_mode():
    reset_execution_policy()
    policy = get_execution_policy()
    assert policy.mode == "cpu"
    assert policy.max_parallel_reviews == 1
    assert policy.max_parallel_analyzers == 1
    assert policy.max_parallel_llm_requests == 1


def test_set_execution_policy_overrides_the_singleton():
    try:
        set_execution_policy(ExecutionPolicy(mode="gpu", max_parallel_reviews=2,
                                              max_parallel_analyzers=2, max_parallel_llm_requests=2))
        assert get_execution_policy().mode == "gpu"
    finally:
        reset_execution_policy()
