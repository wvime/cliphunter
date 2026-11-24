"""What calibrate learns from reasons and publishing results.

This is not a fixed rule list. The notes go back into the next judge packet
and the keep-threshold moves only when the rejects say the same thing.
"""

from __future__ import annotations

from collections import Counter


def summarize_feedback(
    decisions: list[dict],
    publish_rows: list[dict],
    *,
    minimum: float = 65,
) -> dict:
    reasons = Counter()
    categories_approved = Counter()
    categories_rejected = Counter()
    for row in decisions:
        action = row.get("action")
        reason = (row.get("reason") or "").strip()
        category = (row.get("category") or "").strip()
        if action == "reject" and reason:
            reasons[reason] += 1
        if category and action == "approve":
            categories_approved[category] += 1
        if category and action == "reject":
            categories_rejected[category] += 1
    retention: dict[str, list[float]] = {}
    for row in publish_rows:
        value = row.get("retention")
        category = (row.get("category") or row.get("platform") or "").strip()
        if value is None or not category:
            continue
        retention.setdefault(category, []).append(float(value))
    notes: list[str] = []
    new_minimum = minimum
    boring = reasons.get("aburrido", 0) + reasons.get("sin_contexto", 0)
    if boring >= 3 and boring >= reasons.get("mal_cortado", 0):
        new_minimum = min(80.0, round(minimum + 3, 1))
        notes.append(
            f"Sube el mínimo de {minimum:g} a {new_minimum:g}: varios rechazos fueron aburrido o sin contexto."
        )
    elif reasons.get("mal_cortado", 0) >= 3:
        notes.append("Los rechazos recientes son de corte, no de tema. El mínimo no se mueve; hay que afinar los bordes.")
    for category, values in retention.items():
        if not values:
            continue
        mean = sum(values) / len(values)
        notes.append(f"Publicado en {category}: retención media {mean:.2f} sobre {len(values)}.")
    if not notes and not reasons:
        notes.append("Todavía no hay razones ni resultados de publicación.")
    return {
        "minimum": new_minimum,
        "reasons": dict(reasons),
        "approved_categories": dict(categories_approved),
        "rejected_categories": dict(categories_rejected),
        "notes": notes,
    }


def feedback_block(summary: dict) -> str:
    lines = [f"Mínimo aprendido: {summary.get('minimum')}"]
    reasons = summary.get("reasons") or {}
    if reasons:
        rendered = ", ".join(f"{name} {count}" for name, count in reasons.items())
        lines.append(f"Rechazos con razón: {rendered}.")
    for note in summary.get("notes") or []:
        lines.append(note)
    return "\n".join(lines)
