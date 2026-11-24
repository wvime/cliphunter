"""Side-by-side of the signal selector and the editorial selector."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Pick:
    start: float
    end: float
    score: float | None = None
    trigger: str = ""
    category: str | None = None
    title: str | None = None
    tier: str | None = None
    why: str | None = None
    accepted: bool = True
    participants: str = ""
    extra: dict = field(default_factory=dict)

    def overlaps(self, other: Pick) -> bool:
        return self.end > other.start and other.end > self.start


def render_report(old: list[Pick], new: list[Pick], *, rejected: list[Pick] | None = None) -> str:
    rejected = rejected or [pick for pick in new if not pick.accepted]
    accepted = [pick for pick in new if pick.accepted]
    lines = ["# Comparación: señal contra editor", ""]
    lines.append("## Selector viejo (solo señales)")
    lines.extend(_section(old) or ["Ningún corte."])
    lines.append("")
    lines.append("## Selector editorial")
    lines.extend(_section(accepted) or ["Ningún corte."])
    lines.append("")
    lines.append("## Solapes")
    overlaps = []
    for left in old:
        for right in accepted:
            if left.overlaps(right):
                overlaps.append(
                    f"- {left.start:.1f}–{left.end:.1f}s coincide con {right.start:.1f}–{right.end:.1f}s"
                    + (f" ({right.title})" if right.title else "")
                )
    lines.extend(overlaps or ["Ninguno."])
    lines.append("")
    lines.append("## El viejo los corta y el nuevo los rechaza")
    dropped = []
    for left in old:
        reasons = [item for item in rejected if left.overlaps(item)]
        if reasons:
            why = reasons[0].why or "el editor lo rechazó"
            dropped.append(f"- {left.start:.1f}–{left.end:.1f}s ({left.trigger}): {why}")
        elif accepted and not any(left.overlaps(item) for item in accepted):
            dropped.append(
                f"- {left.start:.1f}–{left.end:.1f}s ({left.trigger}): el editor no lo eligió"
            )
    lines.extend(dropped or ["Ninguno."])
    lines.append("")
    lines.append("## El nuevo los encuentra y el viejo no")
    missed = []
    for right in accepted:
        if not any(right.overlaps(left) for left in old):
            label = right.title or right.category or "sin título"
            missed.append(
                f"- {right.start:.1f}–{right.end:.1f}s {label}: {right.why or right.trigger or 'momento quieto'}"
            )
    lines.extend(missed or ["Ninguno."])
    lines.append("")
    return "\n".join(lines)


def render_table(picks: list[Pick]) -> str:
    """Every judged candidate, including the ones that were dropped."""

    header = "| inicio | fin | score | categoría | participantes | por qué | estado |"
    rule = "| --- | --- | --- | --- | --- | --- | --- |"
    rows = [header, rule]
    if not picks:
        rows.append("| — | — | — | — | — | ningún candidato | — |")
        return "\n".join(rows)
    for pick in picks:
        state = "se queda" if pick.accepted else "se descarta"
        score = "" if pick.score is None else f"{pick.score:g}"
        rows.append(
            "| {start:.1f} | {end:.1f} | {score} | {category} | {people} | {why} | {state} |".format(
                start=pick.start,
                end=pick.end,
                score=score,
                category=pick.category or "",
                people=pick.participants,
                why=(pick.why or pick.trigger or "").replace("|", "/"),
                state=state,
            )
        )
    return "\n".join(rows)


def live_sim_windows(
    duration: float,
    *,
    sweep: float = 30,
    before: float = 90,
    after: float = 30,
    cooldown: float = 45,
) -> list[tuple[float, float, float]]:
    """Windows the live loop would judge: every ``sweep`` s, with a cooldown."""

    t = 0.0
    next_ok = 0.0
    windows: list[tuple[float, float, float]] = []
    while t <= duration + 1e-6:
        if t >= next_ok and t > 0:
            windows.append((max(0.0, t - before), min(duration, t + after), t))
            next_ok = t + cooldown
        t += sweep
    return windows


def picks_from_directory(path: Path, *, probe) -> list[Pick]:
    """Treat consecutive files in a folder as the old fixed-length selection."""

    files = sorted(
        item
        for item in path.iterdir()
        if item.is_file() and item.suffix.lower() in {".mp4", ".ts", ".mkv", ".m4v"}
    )
    cursor = 0.0
    picks: list[Pick] = []
    for item in files:
        try:
            duration = float(probe(item) or 0)
        except (TypeError, ValueError):
            duration = 0.0
        if duration <= 0:
            duration = 30.0
        picks.append(
            Pick(
                start=cursor,
                end=cursor + duration,
                trigger=f"clip viejo {item.name}",
                accepted=True,
            )
        )
        cursor += duration
    return picks


def energy_picks(samples: list[float], *, rate: int = 8000, window: float = 10, step: float = 2, threshold: float = 2.5) -> list[Pick]:
    """Old selector with only audio energy, for a VOD that has no chat file."""

    from cliphunter.signals.aggregate import MomentTracker
    from cliphunter.signals.baseline import RollingBaseline

    if rate <= 0 or not samples:
        return []
    hop = max(1, int(step * rate))
    width = max(hop, int(window * rate))
    baseline = RollingBaseline(horizon=600, min_samples=5)
    tracker = MomentTracker()
    found: list[Pick] = []
    t = 0.0
    pos = 0
    while pos + 16 < len(samples):
        chunk = samples[max(0, pos + hop - width) : pos + hop]
        if not chunk:
            break
        rms = (sum(sample * sample for sample in chunk) / len(chunk)) ** 0.5
        score = baseline.add(t, rms)
        phase = tracker.push(t, score, threshold=threshold, release=threshold * 0.65, max_len=45)
        if phase == "fire" and tracker.samples:
            start = tracker.samples[0][0]
            found.append(Pick(start=max(0.0, start - 8), end=t, score=round(score, 3), trigger="audio", accepted=True))
            tracker.reset()
        t += step
        pos += hop
    return found


def _section(picks: list[Pick]) -> list[str]:
    rows = []
    for pick in picks:
        bits = [f"- {pick.start:.1f}–{pick.end:.1f}s"]
        if pick.score is not None:
            bits.append(f"score {pick.score:g}")
        if pick.trigger:
            bits.append(pick.trigger)
        if pick.category:
            bits.append(pick.category)
        if pick.tier:
            bits.append(pick.tier)
        if pick.title:
            bits.append(f"«{pick.title}»")
        if pick.why:
            bits.append(pick.why)
        rows.append(" · ".join(bits))
    return rows


def old_picks_from_moments(moments: list[dict]) -> list[Pick]:
    picks = []
    for moment in moments:
        picks.append(
            Pick(
                start=float(moment["start"]),
                end=float(moment["end"]),
                score=moment.get("score"),
                trigger="señal",
                accepted=True,
            )
        )
    return picks


def picks_from_verdicts(items: list[dict]) -> tuple[list[Pick], list[Pick]]:
    accepted: list[Pick] = []
    rejected: list[Pick] = []
    for item in items:
        pick = Pick(
            start=float(item["start"]),
            end=float(item["end"]),
            score=item.get("score"),
            trigger=str(item.get("trigger") or "editorial"),
            category=item.get("category"),
            title=item.get("title"),
            tier=item.get("tier"),
            why=item.get("why"),
            accepted=bool(item.get("accepted", True)),
        )
        (accepted if pick.accepted else rejected).append(pick)
    return accepted, rejected
