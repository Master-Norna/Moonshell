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

OPTIONAL_SPRITES = (
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
    # "cool" magic batch from image3
    "read",      # reading a book (cozy company while you work)
    "magic",     # casting, sparkle ring
    "flame",     # purple flame aura (fired up)
    "twirl",     # joyful spin
    "moon",      # riding the crescent moon (dreamy / night)
    "star",      # holding up a glowing star (a little gift)
    "dash",      # running with a motion trail (fast approach)
    "poof",      # vanishing in a puff of smoke (collapse transition)
    # second expression batch (image.png) -- same pixel pipeline
    "wink",      # one eye closed + a little star (playful)
    "look_side", # head turned to one side (glances toward your cursor)
    "write",     # writing/studying (cozy company while you work)
    "yawn",      # big sleepy yawn (drowsy beats)
    "teleport",  # swirl of light (alt vanish transition)
    "question",  # holding a little umbrella, puzzled (curious beat)
    "hide",      # ducking down shyly (alt peek)
    "gift",      # holding out a wrapped gift (a rare treat)
    "crystal",   # cradling a glowing crystal (a little treasure)
    # extra walk frames -> the walk auto-uses however many walk_{dir}_N exist
    # (2/3/4-frame cycle).  Current art is a 4-frame side cycle.
    "walk_right_3",
    "walk_left_3",
    "walk_right_4",
    "walk_left_4",
)

LAYOUT_Y_OFFSETS = (-16, -12, -6, -4, -2, 0, 4)
