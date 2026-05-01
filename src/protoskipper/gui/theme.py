# Copyright (C) 2026 DataSailors Pvt Ltd. Licensed under GPL-3.0-or-later.
"""Centralised colour palette and density tokens for the GUI.

Themes are swappable; widgets must NOT hardcode colours. To add a theme,
extend the :class:`Theme` constants and pick one in
:func:`active_theme`.
"""
from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtGui import QColor

from protoskipper.core.driver import Quality, SessionProfile


@dataclass(frozen=True)
class Theme:
    """A flat palette + density profile."""

    name: str

    # Quality colours - watchlist text, packet view background hints, etc.
    quality_good: QColor
    quality_uncertain: QColor
    quality_bad: QColor
    quality_simulated: QColor
    quality_unknown: QColor

    # Profile chip colours - the toolbar indicator the operator looks at
    # before clicking anything destructive.
    profile_lab: QColor
    profile_commissioning: QColor
    profile_production: QColor

    # Direction colours for the packet view.
    direction_tx: QColor
    direction_rx: QColor

    def quality_color(self, quality: Quality) -> QColor:
        return _QUALITY_MAP[self][quality]

    def profile_color(self, profile: SessionProfile) -> QColor:
        return {
            SessionProfile.LAB: self.profile_lab,
            SessionProfile.COMMISSIONING: self.profile_commissioning,
            SessionProfile.PRODUCTION: self.profile_production,
        }[profile]


LIGHT_THEME = Theme(
    name="light",
    quality_good=QColor("#1f2937"),       # near-black
    quality_uncertain=QColor("#b45309"),  # amber-700
    quality_bad=QColor("#b91c1c"),        # red-700
    quality_simulated=QColor("#1d4ed8"),  # blue-700
    quality_unknown=QColor("#6b7280"),    # gray-500
    profile_lab=QColor("#16a34a"),        # green-600
    profile_commissioning=QColor("#d97706"),  # amber-600
    profile_production=QColor("#dc2626"),     # red-600
    direction_tx=QColor("#1d4ed8"),
    direction_rx=QColor("#16a34a"),
)


DARK_THEME = Theme(
    name="dark",
    quality_good=QColor("#e5e7eb"),
    quality_uncertain=QColor("#fbbf24"),
    quality_bad=QColor("#f87171"),
    quality_simulated=QColor("#60a5fa"),
    quality_unknown=QColor("#9ca3af"),
    profile_lab=QColor("#22c55e"),
    profile_commissioning=QColor("#fb923c"),
    profile_production=QColor("#ef4444"),
    direction_tx=QColor("#60a5fa"),
    direction_rx=QColor("#34d399"),
)


_QUALITY_MAP = {
    LIGHT_THEME: {
        Quality.GOOD: LIGHT_THEME.quality_good,
        Quality.UNCERTAIN: LIGHT_THEME.quality_uncertain,
        Quality.BAD: LIGHT_THEME.quality_bad,
        Quality.TIMEOUT: LIGHT_THEME.quality_bad,
        Quality.SIMULATED: LIGHT_THEME.quality_simulated,
        Quality.UNKNOWN: LIGHT_THEME.quality_unknown,
    },
    DARK_THEME: {
        Quality.GOOD: DARK_THEME.quality_good,
        Quality.UNCERTAIN: DARK_THEME.quality_uncertain,
        Quality.BAD: DARK_THEME.quality_bad,
        Quality.TIMEOUT: DARK_THEME.quality_bad,
        Quality.SIMULATED: DARK_THEME.quality_simulated,
        Quality.UNKNOWN: DARK_THEME.quality_unknown,
    },
}


_active = LIGHT_THEME


def active_theme() -> Theme:
    """Return the currently selected theme. v1 ships LIGHT only; the
    function exists so widgets don't have to be revisited when DARK lands."""
    return _active


def set_active_theme(theme: Theme) -> None:
    """Switch the active theme. Widgets that have already painted will
    not pick up the change until the next paint event; emit the relevant
    application-state signal to force a repaint when called at runtime."""
    global _active
    _active = theme
