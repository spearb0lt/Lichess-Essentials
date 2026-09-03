"""One colour per sideline.

Every sideline in a chapter is handed its own colour, so two alternatives to
the same move never look alike.  A colour arrives in three tones that always
belong together:

``rule``
    the vertical bar drawn beside a board or a notation block.
``ink``
    the move text of that sideline.
``tint``
    a very light wash for the background behind it, used where there is no
    room for a bar.

The three tones share a hue and are solved for a *fixed relative luminance*,
which is what keeps them readable: every ``ink`` sits at roughly 6:1 against
white and 4.8:1 against its own ``tint``, no matter which hue it is.  Yellows
therefore come out olive and cyans come out teal rather than washing out.

Uniform luminance has one consequence worth stating plainly: printed in
greyscale all sideline colours collapse to the same grey.  Nesting still
reads -- the bar, the indent and the depth dots are all shape, not colour --
and identity still reads too, because every sideline also carries its number
(``s3``).  Colour is the fast cue, not the only one.
"""

from __future__ import annotations

import colorsys
from dataclasses import dataclass

#: How many distinct sideline colours exist before they start repeating.
#: A branch point with more alternatives than this is unheard of; a whole
#: chapter with more sidelines than this is common, and that is fine -- the
#: colour only has to separate sidelines a reader sees at the same time.
PALETTE_SIZE = 24

#: The 24 hues are an exact 15-degree sweep of the wheel, walked in steps of
#: seven slots (7 and 24 are coprime, so all 24 are used).  Siblings at one
#: branch point take *consecutive* slots, so it is neighbours in this sequence
#: that must look most different: consecutive slots land 105 degrees apart,
#: and no two slots within a run of seven are closer than 45 degrees.
_HUE_STEP = 7

_HUES = tuple((index * _HUE_STEP * (360 // PALETTE_SIZE)) % 360.0
              for index in range(PALETTE_SIZE))

#: Hue alone, at one fixed luminance, is a single cue.  Alternate slots are
#: muted or vivid as well, which separates neighbours a second way -- and
#: unlike a lightness change it costs nothing in contrast, because luminance
#: is held fixed either way.
_MUTED, _VIVID = 0.45, 0.78

#: Washes stay pale whatever the ink does: at 88% luminance a saturated green
#: or yellow turns neon, and the hue is identity enough for a background.
_TINT_SATURATION = 0.45

# Lightness is solved for, per hue, to hit these luminances.
_INK_LUMINANCE = 0.12     # ~6:1 on white, ~4.8:1 on this slot's own tint
_RULE_LUMINANCE = 0.22    # a visible graphic bar
_TINT_LUMINANCE = 0.80    # a pale wash: tellable apart, never loud


def _relative_luminance(rgb) -> float:
    """WCAG relative luminance of an (r, g, b) triple in 0..1."""

    def channel(value: float) -> float:
        return value / 12.92 if value <= 0.03928 else ((value + 0.055) / 1.055) ** 2.4

    red, green, blue = (channel(c) for c in rgb)
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def _at_luminance(hue: float, saturation: float, target: float) -> str:
    """The hex colour of ``hue`` sitting at ``target`` relative luminance.

    Luminance rises monotonically with HLS lightness at a fixed hue and
    saturation, so a plain bisection lands on it.
    """
    low, high = 0.0, 1.0
    for _ in range(40):
        middle = (low + high) / 2.0
        if _relative_luminance(colorsys.hls_to_rgb(hue / 360.0, middle, saturation)) < target:
            low = middle
        else:
            high = middle
    rgb = colorsys.hls_to_rgb(hue / 360.0, (low + high) / 2.0, saturation)
    return "#%02x%02x%02x" % tuple(round(255 * c) for c in rgb)


@dataclass(frozen=True)
class SidelineColor:
    """The three tones of one sideline colour, as ``#rrggbb`` strings."""

    slot: int          # 0-based index into PALETTE
    ink: str           # move text
    rule: str          # the bar beside a board or block
    tint: str          # background wash

    @property
    def latex_name(self) -> str:
        return "lspside%d" % self.slot


def _slot(slot: int, hue: float) -> "SidelineColor":
    saturation = _VIVID if slot % 2 else _MUTED
    return SidelineColor(
        slot=slot,
        ink=_at_luminance(hue, saturation, _INK_LUMINANCE),
        rule=_at_luminance(hue, min(0.85, saturation + 0.06), _RULE_LUMINANCE),
        tint=_at_luminance(hue, _TINT_SATURATION, _TINT_LUMINANCE),
    )


PALETTE = tuple(_slot(slot, hue) for slot, hue in enumerate(_HUES))


def slot_for(branch: int) -> int:
    """Palette slot of sideline number ``branch`` (1-based)."""
    return (max(1, branch) - 1) % PALETTE_SIZE


def color_for(branch: int) -> SidelineColor | None:
    """The colour of sideline ``branch``, or ``None`` for the main line."""
    if not branch:
        return None
    return PALETTE[slot_for(branch)]


def tag_for(branch: int) -> str:
    """Short printed name of a sideline, so identity survives greyscale."""
    return "s%d" % branch if branch else "main"
