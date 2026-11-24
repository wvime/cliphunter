"""9:16 master with Dominican Spanish subtitles and the title box.

Used only after a human approves. The review file stays the precise cut.
"""

from __future__ import annotations

from pathlib import Path

from cliphunter.processing.title_overlay import TitleStyle, plan_title_spans
from cliphunter.procutil import run_process

from cliphunter.editorial.bounds import Line


def title_markup(title: str, sujeto: str | None) -> str:
    text = " ".join((title or "").split())
    name = (sujeto or "").strip()
    if not text and name:
        return f"*{name}*"
    if name and name.casefold() in text.casefold():
        start = text.casefold().find(name.casefold())
        return text[:start] + "*" + text[start : start + len(name)] + "*" + text[start + len(name) :]
    if name:
        return f"*{name}* {text}".strip()
    return text


def ass_time(seconds: float) -> str:
    seconds = max(0.0, seconds)
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    whole = int(seconds % 60)
    centis = int(round((seconds - int(seconds)) * 100))
    if centis == 100:
        centis = 0
        whole += 1
    return f"{hours}:{minutes:02d}:{whole:02d}.{centis:02d}"


def _ass_color(hex_color: str) -> str:
    text = hex_color.removeprefix("#")
    red, green, blue = text[0:2], text[2:4], text[4:6]
    return f"&H00{blue}{green}{red}&".upper()


def build_ass(
    *,
    title: str,
    sujeto: str | None,
    lines: list[Line],
    clip_start: float,
    style: TitleStyle,
    duration: float,
) -> str:
    title_text = title_markup(title, sujeto)
    spans = plan_title_spans(title_text, sujeto=sujeto) if title_text else []
    title_body = ""
    for span in spans:
        if span.highlight and style.highlight_mode == "text":
            title_body += f"{{\\c{_ass_color(style.highlight_color)}}}{ _ass_escape(span.text) }{{\\c{_ass_color(style.text_color)}}}"
        elif span.highlight:
            title_body += f"{{\\3c{_ass_color(style.highlight_color)}\\bord12}}{ _ass_escape(span.text) }{{\\3c{_ass_color(style.box_color)}\\bord18}}"
        else:
            title_body += _ass_escape(span.text)
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 0

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Title,Montserrat Black,62,{_ass_color(style.text_color)},&H000000FF,{_ass_color(style.box_color)},{_ass_color(style.box_color)},-1,0,0,0,100,100,0,0,3,18,0,8,70,70,90,1
Style: Sub,Arial,52,&H00FFFFFF,&H000000FF,&H00000000,&H64000000,0,0,0,0,100,100,0,0,1,3,0,2,50,50,150,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    events = []
    if title_body.strip():
        events.append(f"Dialogue: 1,{ass_time(0)},{ass_time(max(duration, 0.5))},Title,,0,0,0,,{title_body}")
    for line in lines:
        start = max(0.0, line.start - clip_start)
        end = max(start + 0.3, line.end - clip_start)
        text = _ass_escape(line.text.strip())
        if not text:
            continue
        events.append(f"Dialogue: 0,{ass_time(start)},{ass_time(end)},Sub,,0,0,0,,{text}")
    return header + "\n".join(events) + "\n"


def _ass_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}").replace("\n", "\\N")


def _filter_path(path: Path) -> str:
    text = path.resolve().as_posix().replace("\\", "/").replace(":", "\\:")
    return text.replace("'", "\\'")


async def render_vertical(
    source: Path,
    dest: Path,
    *,
    title: str,
    sujeto: str | None,
    lines: list[Line],
    clip_start: float,
    duration: float,
    style: TitleStyle | None = None,
) -> None:
    style = style or TitleStyle()
    dest.parent.mkdir(parents=True, exist_ok=True)
    ass_path = dest.with_suffix(".ass")
    ass_path.write_text(
        build_ass(
            title=title,
            sujeto=sujeto,
            lines=lines,
            clip_start=clip_start,
            style=style,
            duration=duration,
        ),
        encoding="utf-8",
    )
    fonts = style.ass_fonts_dir()
    sub = f"subtitles='{_filter_path(ass_path)}':fontsdir='{_filter_path(Path(fonts))}'"
    video = f"scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,{sub}"
    code, error = await run_process(
        [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-threads",
            "1",
            "-i",
            str(source),
            "-vf",
            video,
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "23",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-movflags",
            "+faststart",
            str(dest),
        ]
    )
    if code != 0 or not dest.exists() or dest.stat().st_size == 0:
        raise RuntimeError((error or "no se pudo renderizar el 9:16")[-800:])
