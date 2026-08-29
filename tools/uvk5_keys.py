#!/usr/bin/env python3
"""Key names the keypad model accepts.

Mirrors keypad_key_names in qemu/py32f071.c. test_uvk5_keys.py parses that array
out of the model source and fails if the two drift apart.

PTT is absent on purpose, and still is now that the model supports it: the firmware
reads PB10 directly rather than scanning PTT as a matrix key, so it is exposed as its
own boolean property and its own endpoint (POST /api/ptt) instead of a name here. The
"press" property rejects "PTT".
"""

KEYS = (
    "MENU", "UP", "DOWN", "EXIT", "F", "STAR",
    "0", "1", "2", "3", "4", "5", "6", "7", "8", "9",
    "SIDE1", "SIDE2",
)


def normalise(name) -> str:
    return (name or "").strip().upper()


def is_valid(name) -> bool:
    return normalise(name) in KEYS
