from __future__ import annotations

SPRITE_SIZE = 96
STAGE_SIZE = 144
SPRITE_X = 24
SPRITE_Y = 32
MAX_UP_OFFSET = -16
MAX_DOWN_OFFSET = 4

REQUIRED_SPRITES = (
    "idle",
    "blink",
    "happy",
    "curious",
    "sleepy",
    "peek",
    "walk_right_1",
    "walk_right_2",
    "walk_left_1",
    "walk_left_2",
    "notify",
    "hover",
)

# Coherent expression set that is ready for the runtime and release build.
OPTIONAL_BASE_SPRITES = (
    "wave",
    "shy",
    "pout",
    "sad",
    "excited",
    "love",
    "surprised",
    "sleep",
    "dizzy",
    "sit",
)

FEATURE_SPRITES = (
    "read",
    "magic",
    "star",
)

# The required set contains frames 1-2 in both directions. These complete the
# coherent four-frame side walk used by the active release.
EXTRA_WALK_SPRITES = (
    "walk_right_3",
    "walk_left_3",
    "walk_right_4",
    "walk_left_4",
)

OPTIONAL_SPRITES = (
    *OPTIONAL_BASE_SPRITES,
    *FEATURE_SPRITES,
    *EXTRA_WALK_SPRITES,
)

# Runtime and release tooling must use this allowlist, never a directory glob.
# Retired PNGs remain beside the active sources so they can be reworked without
# silently returning to the product or influencing the shared palette.
ACTIVE_SPRITES = (*REQUIRED_SPRITES, *OPTIONAL_SPRITES)

# Paused until their silhouettes and motion language are rebuilt as coherent
# multi-frame actions. Source and generated PNG files intentionally stay intact.
RETIRED_PAUSED_SPRITES = (
    "flame",
    "twirl",
    "moon",
    "dash",
    "poof",
    "wink",
    "look_side",
    "yawn",
    "teleport",
    "question",
    "hide",
)

# Useful concepts whose current props/readability need a fresh art pass.
RETIRED_REDO_SPRITES = (
    "write",
    "gift",
    "crystal",
)

RETIRED_SPRITES = (*RETIRED_PAUSED_SPRITES, *RETIRED_REDO_SPRITES)

LAYOUT_Y_OFFSETS = (-16, -12, -6, -4, -2, 0, 4)
