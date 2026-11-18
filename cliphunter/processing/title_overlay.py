"""Design contract for the burned-in viral title. Nothing is rendered yet.

After a clip is approved, a later phase will reframe to 9:16 and paint a
rounded white box near the top, above the picture, with bold black text.
The person the clip is about comes from Claude's ``sujeto`` field and is
marked in ``title_format`` so that word can be highlighted.

Planned renderer, CPU only, Windows-friendly:

* ``ass`` (default): one ASS subtitle. A drawing or blur-edged shape makes
  the rounded white box; ``{\\c&H000000&}`` is the black text and a yellow
  ``{\\3c}`` or ``{\\c}`` run marks ``sujeto``. ffmpeg burns it with
  ``-vf ass=`` or the ``subtitles`` filter, ``-threads 1``.
* ``drawtext``: fallback. ``drawbox`` for the white panel (square corners)
  plus two ``drawtext`` passes, the second only over the highlighted name.
  ``fontfile`` is the absolute path of the bundled Montserrat Black
  ``.ttf`` (or whatever ``font_file`` points at).

``burn_title`` raises until that phase. Planning the spans is implemented
now so the scorer and the renderer share one markup.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

_NAMED_COLORS = {
    "white": "#FFFFFF",
    "black": "#000000",
    "yellow": "#FFD400",
}
_STAR = re.compile(r"\*([^*]+)\*")
_SUBJECT = "{sujeto}"
_BUNDLED_FONT = Path("assets") / "fonts" / "Montserrat-Black.ttf"


def bundled_font_path() -> Path:
    """Absolute path to the OFL Montserrat Black file shipped with the repo."""

    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / _BUNDLED_FONT
        if candidate.is_file():
            return candidate
    return here.parents[2] / _BUNDLED_FONT


def resolve_font_file(font_file: str | None) -> Path:
    """Turn a config path into an absolute file path for ffmpeg and ASS.

    Relative paths are tried from the repo root (next to ``assets/``) and
    then from the current directory. ``as_posix()`` on the result is what
    ffmpeg should receive, including on Windows.
    """

    if not font_file:
        return bundled_font_path()
    path = Path(font_file)
    if path.is_absolute():
        return path
    root = _project_root()
    if root is not None:
        from_root = (root / path).resolve()
        if from_root.is_file():
            return from_root
    from_cwd = (Path.cwd() / path).resolve()
    if from_cwd.is_file():
        return from_cwd
    if root is not None:
        return (root / path).resolve()
    return from_cwd


def _project_root() -> Path | None:
    for parent in Path(__file__).resolve().parents:
        if (parent / "assets").is_dir():
            return parent
    return None


class TitleSpan(BaseModel):
    text: str
    highlight: bool = False


class TitleStyle(BaseModel):
    """How the approved vertical clip will wear its title. Not applied yet."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    box_color: str = "#FFFFFF"
    text_color: str = "#000000"
    highlight_color: str = "#FFD400"
    highlight_mode: Literal["background", "text"] = "background"
    font: str = "Montserrat Black"
    font_file: str | None = "assets/fonts/Montserrat-Black.ttf"
    position: Literal["top"] = "top"
    corner_radius: int = Field(default=16, ge=0, le=80)
    title_format: str = "*{sujeto}*"
    renderer: Literal["ass", "drawtext"] = "ass"

    @field_validator("box_color", "text_color", "highlight_color")
    @classmethod
    def _color(cls, value: str) -> str:
        text = value.strip().lower()
        if text in _NAMED_COLORS:
            return _NAMED_COLORS[text]
        if re.fullmatch(r"#[0-9a-f]{6}", text):
            return text.upper()
        raise ValueError("color must be #RRGGBB or white, black, yellow")

    @field_validator("font_file", mode="before")
    @classmethod
    def _blank_font(cls, value: object) -> object:
        if value is None or (isinstance(value, str) and not value.strip()):
            return None
        return value

    @field_validator("title_format")
    @classmethod
    def _format(cls, value: str) -> str:
        if _SUBJECT not in value and not _STAR.search(value):
            raise ValueError("title_format must mark the subject with {sujeto} or *Nombre*")
        return value

    def resolved_font_file(self) -> Path:
        return resolve_font_file(self.font_file)

    def ffmpeg_fontfile(self) -> str:
        """Absolute font path with forward slashes, for ``drawtext=fontfile=``."""

        return self.resolved_font_file().as_posix()

    def ass_fonts_dir(self) -> str:
        """Directory ffmpeg's ``fontsdir`` option should search for this face."""

        return self.resolved_font_file().parent.as_posix()


def plan_title_spans(template: str, sujeto: str | None = None) -> list[TitleSpan]:
    """Split a title into plain and highlighted runs.

    ``{sujeto}`` is replaced with Claude's name and that run is highlighted.
    ``*Nombre*`` highlights whatever is inside the asterisks. Asterisks are
    markup, not characters in the burned title.
    """

    name = (sujeto or "").strip()
    text = template
    if _SUBJECT in text:
        if name:
            wrapped = f"*{_SUBJECT}*" in text
            text = text.replace(_SUBJECT, name)
            if not wrapped:
                text = text.replace(name, f"*{name}*", 1)
        elif f"*{_SUBJECT}*" not in text:
            text = text.replace(_SUBJECT, f"*{_SUBJECT}*", 1)

    spans: list[TitleSpan] = []
    cursor = 0
    for match in _STAR.finditer(text):
        if match.start() > cursor:
            spans.append(TitleSpan(text=text[cursor : match.start()], highlight=False))
        spans.append(TitleSpan(text=match.group(1), highlight=True))
        cursor = match.end()
    if cursor < len(text):
        spans.append(TitleSpan(text=text[cursor:], highlight=False))
    return [span for span in spans if span.text]


class PendingTitleOverlay:
    """Placeholder. Call ``plan_title_spans`` today; burning waits for approval processing."""

    def __init__(self, style: TitleStyle | None = None) -> None:
        self.style = style or TitleStyle()

    async def burn_title(self, source: Path, dest: Path, sujeto: str | None = None) -> Path:
        del source, dest, sujeto
        raise NotImplementedError(
            "El título sobre el 9:16 se quema al aprobar, en una fase posterior "
            f"({self.style.renderer}, caja {self.style.box_color}, "
            f"texto {self.style.text_color}, resalte {self.style.highlight_color})."
        )
