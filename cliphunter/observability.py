"""Per-stream CPU and RSS samples, as JSON logs and rows in ``metrics``."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

import psutil

from cliphunter.bus import METRICS_SAMPLE, Event, EventBus
from cliphunter.jsonlog import log_event
from cliphunter.storage.repo import Repo


class MetricsSampler:
    """``system_cpu_pct`` is the whole machine (0–100).

    Per-process figures come from psutil and can exceed 100 when a process
    uses more than one core. Compare the laptop budget with ``system_cpu_pct``.
    """

    def __init__(
        self,
        *,
        repo: Repo,
        interval: float,
        streams: Callable[[], list[dict]],
        bus: EventBus | None = None,
    ) -> None:
        self.repo = repo
        self.interval = interval
        self.streams = streams
        self.bus = bus
        self._process = psutil.Process()
        self._children: dict[int, psutil.Process] = {}

    async def run(self, stop: asyncio.Event) -> None:
        try:
            self._process.cpu_percent(None)
            psutil.cpu_percent(None)
        except Exception as exc:
            log_event("ERROR", "metrics.unavailable", error=str(exc))
            return
        while not stop.is_set():
            try:
                await self.sample()
            except Exception as exc:
                log_event("ERROR", "metrics.failed", error=str(exc))
            try:
                await asyncio.wait_for(stop.wait(), timeout=self.interval)
                return
            except TimeoutError:
                continue

    async def sample(self) -> dict:
        system_cpu = psutil.cpu_percent(None)
        process_cpu = self._process.cpu_percent(None)
        process_rss = self._process.memory_info().rss / (1024 * 1024)
        reports: list[dict] = []
        live_pids: set[int] = set()
        for stream in self.streams():
            pids = [int(pid) for pid in stream.get("pids") or []]
            live_pids.update(pids)
            cpu = 0.0
            rss = 0.0
            for pid in pids:
                stats = self._child_stats(pid)
                if stats is None:
                    continue
                child_cpu, child_rss = stats
                cpu += child_cpu
                rss += child_rss
            report = {
                "channel_key": stream.get("channel_key"),
                "session_id": stream.get("session_id"),
                "cpu_pct": round(cpu, 2),
                "rss_mb": round(rss, 2),
                "pids": pids,
            }
            reports.append(report)
            session_id = stream.get("session_id")
            if session_id:
                await self.repo.insert_metric(
                    session_id=int(session_id),
                    cpu_pct=report["cpu_pct"],
                    rss_mb=report["rss_mb"],
                    extra={
                        "system_cpu_pct": system_cpu,
                        "process_cpu_pct": round(process_cpu, 2),
                        "process_rss_mb": round(process_rss, 2),
                        "channel_key": stream.get("channel_key"),
                    },
                )
        for pid in list(self._children):
            if pid not in live_pids:
                self._children.pop(pid, None)
        payload = {
            "system_cpu_pct": system_cpu,
            "process_cpu_pct": round(process_cpu, 2),
            "process_rss_mb": round(process_rss, 2),
            "streams": reports,
        }
        log_event("INFO", "metrics.sample", **payload)
        if self.bus is not None:
            await self.bus.publish(Event(METRICS_SAMPLE, payload))
        return payload

    def _child_stats(self, pid: int) -> tuple[float, float] | None:
        proc = self._children.get(pid)
        if proc is None:
            try:
                proc = psutil.Process(pid)
            except psutil.Error:
                return None
            proc.cpu_percent(None)
            self._children[pid] = proc
        try:
            cpu = float(proc.cpu_percent(None))
            rss = float(proc.memory_info().rss) / (1024 * 1024)
        except psutil.Error:
            self._children.pop(pid, None)
            return None
        return cpu, rss
