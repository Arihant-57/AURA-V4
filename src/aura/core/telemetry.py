"""Non-blocking system hardware telemetry reader (CPU, RAM, GPU, VRAM, battery, Ollama)."""

from __future__ import annotations

import atexit
import os
import subprocess
import sys
import threading
import time
from typing import Any

import psutil


class HardwareTelemetrySampler:
    """Thread-safe non-blocking telemetry sampler using psutil and pynvml."""

    def __init__(self, poll_interval: float = 2.0) -> None:
        self._poll_interval = max(0.5, float(poll_interval))
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._nvml_ready = False
        self._nvml_handle: Any = None
        # Prime psutil delta counter once so subsequent interval=None calls are instant & non-zero.
        try:
            psutil.cpu_percent(interval=None)
        except Exception:
            pass
        self._snapshot: dict[str, Any] = self._collect_sample(ollama_online=None)

    def _init_nvml(self) -> None:
        if self._nvml_ready:
            return
        try:
            import pynvml

            pynvml.nvmlInit()
            if pynvml.nvmlDeviceGetCount() > 0:
                self._nvml_handle = pynvml.nvmlDeviceGetHandleByIndex(0)
                self._nvml_ready = True
        except Exception:
            self._nvml_ready = False
            self._nvml_handle = None

    def _read_gpu(self) -> tuple[float | None, float | None, int | None, int | None, int | None]:
        self._init_nvml()
        if self._nvml_ready and self._nvml_handle is not None:
            try:
                import pynvml

                util = pynvml.nvmlDeviceGetUtilizationRates(self._nvml_handle)
                mem = pynvml.nvmlDeviceGetMemoryInfo(self._nvml_handle)
                used_mb = round(mem.used / (1024 * 1024))
                total_mb = round(mem.total / (1024 * 1024))
                vram_pct = round((mem.used / mem.total) * 100.0, 1) if mem.total > 0 else 0.0
                temp_c: int | None = None
                try:
                    temp_c = int(
                        pynvml.nvmlDeviceGetTemperature(
                            self._nvml_handle, pynvml.NVML_TEMPERATURE_GPU
                        )
                    )
                except Exception:
                    pass
                return float(util.gpu), vram_pct, used_mb, total_mb, temp_c
            except Exception:
                self._nvml_ready = False

        # Fallback to nvidia-smi CLI on Windows/Linux if NVML Python handle fails
        try:
            extra = (
                {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
                if sys.platform == "win32"
                else {}
            )
            out = subprocess.check_output(
                [
                    "nvidia-smi",
                    "--query-gpu=utilization.gpu,memory.used,memory.total,temperature.gpu",
                    "--format=csv,noheader,nounits",
                ],
                text=True,
                timeout=1.5,
                **extra,
            ).strip()
            if out:
                parts = [p.strip() for p in out.splitlines()[0].split(",")]
                if len(parts) >= 3:
                    gpu_u = float(parts[0])
                    used_m = int(float(parts[1]))
                    total_m = int(float(parts[2]))
                    vram_p = round((used_m / total_m) * 100.0, 1) if total_m > 0 else 0.0
                    temp_v = int(float(parts[3])) if len(parts) >= 4 else None
                    return gpu_u, vram_p, used_m, total_m, temp_v
        except Exception:
            pass
        return None, None, None, None, None

    @staticmethod
    def _check_ollama(timeout: float = 0.4) -> bool:
        try:
            from .providers import is_ollama_running

            return bool(is_ollama_running(timeout=timeout))
        except Exception:
            return False

    def _collect_sample(self, *, ollama_online: bool | None = None) -> dict[str, Any]:
        try:
            cpu_pct = round(float(psutil.cpu_percent(interval=None)), 1)
        except Exception:
            cpu_pct = 0.0

        try:
            ram_pct = round(float(psutil.virtual_memory().percent), 1)
        except Exception:
            ram_pct = 0.0

        battery_pct: int | None = None
        power_plugged: bool | None = None
        try:
            bat = psutil.sensors_battery()
            if bat is not None:
                battery_pct = max(0, min(100, round(float(bat.percent))))
                power_plugged = bool(bat.power_plugged)
        except Exception:
            pass

        gpu_pct, vram_pct, used_mb, total_mb, temp_c = self._read_gpu()
        ollama_state = self._check_ollama() if ollama_online is None else bool(ollama_online)

        return {
            "cpu_percent": cpu_pct,
            "ram_percent": ram_pct,
            "gpu_percent": gpu_pct,
            "vram_percent": vram_pct,
            "vram_used_mb": used_mb,
            "vram_total_mb": total_mb,
            "gpu_temp_c": temp_c,
            "battery_percent": battery_pct,
            "power_plugged": power_plugged,
            "ollama_online": ollama_state,
            "timestamp": time.time(),
        }

    def start(self) -> None:
        """Start background daemon thread so HTTP/UI reads never block."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop_event.clear()

            def _loop() -> None:
                while not self._stop_event.wait(self._poll_interval):
                    snap = self._collect_sample()
                    with self._lock:
                        self._snapshot = snap

            t = threading.Thread(target=_loop, name="aura-telemetry", daemon=True)
            t.start()
            self._thread = t

    def stop(self) -> None:
        self._stop_event.set()
        if self._nvml_ready:
            try:
                import pynvml

                pynvml.nvmlShutdown()
            except Exception:
                pass
            self._nvml_ready = False

    def snapshot(self, *, ollama_online: bool | None = None, refresh: bool = False) -> dict[str, Any]:
        """Return latest hardware snapshot in <1ms without blocking the caller."""
        if not os.environ.get("PYTEST_CURRENT_TEST"):
            self.start()
        if refresh:
            snap = self._collect_sample(ollama_online=ollama_online)
            with self._lock:
                self._snapshot = snap
            return dict(snap)
        with self._lock:
            snap = dict(self._snapshot)
        if ollama_online is not None:
            snap["ollama_online"] = bool(ollama_online)
        return snap


_SHARED_SAMPLER: HardwareTelemetrySampler | None = None
_SAMPLER_LOCK = threading.Lock()


def get_hardware_telemetry(
    *,
    ollama_online: bool | None = None,
    refresh: bool = False,
) -> dict[str, Any]:
    """Return cached non-blocking CPU, RAM, GPU, VRAM, battery, and Ollama telemetry."""
    global _SHARED_SAMPLER
    if _SHARED_SAMPLER is None:
        with _SAMPLER_LOCK:
            if _SHARED_SAMPLER is None:
                _SHARED_SAMPLER = HardwareTelemetrySampler()
                atexit.register(_SHARED_SAMPLER.stop)
    return _SHARED_SAMPLER.snapshot(ollama_online=ollama_online, refresh=refresh)
