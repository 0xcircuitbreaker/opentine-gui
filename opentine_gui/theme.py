"""The console's palette.

Colour only — no Dear PyGui import — so the inspectors, the tests and the
lint pass can name a colour without a graphics context existing.
"""

from __future__ import annotations

from opentine.core import RunStatus, StepKind

BRAND = [120, 164, 255]
BRAND_DIM = [84, 117, 184]

SURFACE_APP = [31, 30, 27]
SURFACE_SIDEBAR = [25, 24, 20]
SURFACE_PANEL = [35, 34, 31]
SURFACE_CARD = [39, 38, 34]
SURFACE_INPUT = [31, 30, 27]
SURFACE_BUTTON = [52, 50, 45]

TEXT_PRIMARY = [230, 225, 216]
TEXT_SECONDARY = [184, 177, 166]
TEXT_MUTED = [150, 142, 131]
TEXT_FAINT = [98, 91, 83]

BORDER_DEFAULT = [52, 50, 45]
BORDER_STRONG = [70, 67, 59]
STATE_HOVER = [45, 43, 39]
STATE_SELECTED = [30, 52, 76]
STATE_ACTIVE = [32, 58, 85]

ACCENT_GREEN = [121, 216, 157]
ACCENT_ORANGE = [243, 161, 91]
ACCENT_RED = [255, 138, 134]
ACCENT_PURPLE = [182, 156, 255]
ACCENT_TEAL = [100, 209, 200]
ACCENT_YELLOW = [242, 200, 107]

STEP_COLORS: dict[StepKind, list[int]] = {
    StepKind.think: ACCENT_YELLOW,
    StepKind.tool: BRAND,
    StepKind.model: ACCENT_TEAL,
    StepKind.done: ACCENT_GREEN,
    StepKind.error: ACCENT_RED,
}

RUN_STATUS_COLORS: dict[RunStatus, list[int]] = {
    RunStatus.running: BRAND,
    RunStatus.paused: ACCENT_ORANGE,
    RunStatus.completed: ACCENT_GREEN,
    RunStatus.failed: ACCENT_RED,
}

#: Roles the runtime records, in the colour the DAG already uses for that kind
#: of work, so the transcript and the graph read as one system.
TRANSCRIPT_ROLE_COLORS = {
    "user": TEXT_PRIMARY,
    "assistant": ACCENT_TEAL,
    "tool": BRAND,
    "system": ACCENT_PURPLE,
}

#: Severity colours for the message log, which is the one place the console
#: speaks in its own voice rather than an artifact's.
LEVEL_COLORS = {
    "info": TEXT_SECONDARY,
    "ok": ACCENT_GREEN,
    "warn": ACCENT_ORANGE,
    "error": ACCENT_RED,
}


def _rgba(color: list[int], alpha: int = 255) -> list[int]:
    return [color[0], color[1], color[2], alpha]


def _brighten(color: list[int], amount: int) -> list[int]:
    return [min(255, c + amount) for c in color[:3]]


def _dim(color: list[int], factor: float) -> list[int]:
    return [int(c * factor) for c in color[:3]]
