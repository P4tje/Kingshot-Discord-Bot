"""Shared game-data constants used across cogs.

Single source of truth for the Town Center (TC) level table so cogs reference
one mapping instead of each duplicating it. The raw API field is still named
"stove"/"furnace" upstream; only the display labels are Kingshot's TC scheme.
"""

# Raw API stove level -> in-game display name. Levels <= 30 are plain numbers
# and intentionally aren't listed (callers fall back to "Level N").
LEVEL_MAPPING = {
    31: "30-1", 32: "30-2", 33: "30-3", 34: "30-4",
    35: "TC 1", 36: "TC 1-1", 37: "TC 1-2", 38: "TC 1-3", 39: "TC 1-4",
    40: "TC 2", 41: "TC 2-1", 42: "TC 2-2", 43: "TC 2-3", 44: "TC 2-4",
    45: "TC 3", 46: "TC 3-1", 47: "TC 3-2", 48: "TC 3-3", 49: "TC 3-4",
    50: "TC 4", 51: "TC 4-1", 52: "TC 4-2", 53: "TC 4-3", 54: "TC 4-4",
    55: "TC 5", 56: "TC 5-1", 57: "TC 5-2", 58: "TC 5-3", 59: "TC 5-4",
    60: "TC 6", 61: "TC 6-1", 62: "TC 6-2", 63: "TC 6-3", 64: "TC 6-4",
    65: "TC 7", 66: "TC 7-1", 67: "TC 7-2", 68: "TC 7-3", 69: "TC 7-4",
    70: "TC 8", 71: "TC 8-1", 72: "TC 8-2", 73: "TC 8-3", 74: "TC 8-4",
    75: "TC 9", 76: "TC 9-1", 77: "TC 9-2", 78: "TC 9-3", 79: "TC 9-4",
    80: "TC 10", 81: "TC 10-1", 82: "TC 10-2", 83: "TC 10-3", 84: "TC 10-4",
}

MAX_FURNACE_LEVEL = max(LEVEL_MAPPING)
MAX_STATE = 99999           # ~4 digits today; 5 leaves headroom without accepting junk


def _squash(text: str) -> str:
    """Comparison key, so 'TC 10-2' == 'tc10-2' == 'TC102'."""
    return "".join(c for c in str(text).upper() if c.isalnum())


_LEVEL_LOOKUP = {_squash(name): lv for lv, name in LEVEL_MAPPING.items()}


def format_furnace_level(raw) -> str:
    """Display name for a raw API stove level (e.g. 80 -> 'TC 10'); <= 30 -> 'Level N'."""
    try:
        lv = int(raw)
    except (TypeError, ValueError):
        return str(raw)
    if lv > 30:
        return LEVEL_MAPPING.get(lv, f"Level {lv}")
    return f"Level {lv}"


def parse_furnace_level(text):
    """Raw stove level from a display name ('TC 10-2') or a number ('82'), else None."""
    if text is None:
        return None
    raw = str(text).strip()
    if not raw:
        return None
    if raw.isdigit():
        lv = int(raw)
        return lv if 0 <= lv <= MAX_FURNACE_LEVEL else None
    squashed = _squash(raw)
    if squashed in _LEVEL_LOOKUP:
        return _LEVEL_LOOKUP[squashed]
    # "Level 25" - format_furnace_level's pre-TC output.
    if squashed.startswith("LEVEL"):
        digits = squashed[len("LEVEL"):]
        if digits.isdigit() and int(digits) <= MAX_FURNACE_LEVEL:
            return int(digits)
    return None


def parse_state(text):
    """A typed kingdom as an int in 1..MAX_STATE, or None."""
    raw = str(text).strip() if text is not None else ""
    return int(raw) if raw.isdigit() and 1 <= int(raw) <= MAX_STATE else None
