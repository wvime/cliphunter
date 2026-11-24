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
