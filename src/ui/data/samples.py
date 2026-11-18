"""Sample content for features the emulator core does not implement yet
(achievements, cheats, link play). The views label these as previews."""
from __future__ import annotations

ACHIEVEMENTS = [
    {"name": "First Delivery", "desc": "Complete your first route", "pts": 10, "done": "Today"},
    {"name": "Untouchable", "desc": "Beat a boss without taking damage", "pts": 25, "done": "Today"},
    {"name": "Frequent Flyer", "desc": "Fly 100 routes", "pts": 10, "done": ""},
    {"name": "Collector", "desc": "Find all 40 cargo crates", "pts": 50, "done": ""},
    {"name": "Night Shift", "desc": "Finish a route after midnight", "pts": 5, "done": "Yesterday"},
    {"name": "Speed Demon", "desc": "Finish chapter 2 under 20 minutes", "pts": 25, "done": ""},
    {"name": "Best Friends", "desc": "Win a link-cable race", "pts": 10, "done": "Last week"},
    {"name": "Completionist", "desc": "Reach 100% completion", "pts": 100, "done": ""},
]

CHEATS = [
    {"id": "a", "name": "Infinite health", "code": "82003B74 0064"},
    {"id": "b", "name": "Max coins", "code": "8200C1F0 270F"},
    {"id": "c", "name": "Walk through walls", "code": "[YOUR CODE]"},
    {"id": "d", "name": "All levels unlocked", "code": "42001A00 00FF"},
]

ROOM_CODE = "K7Q-42M"
FRIEND = "[Friend name]"
USERNAME = "[username]"

SETTINGS_SECTIONS = ["General", "Emulation", "Video", "Audio", "Controls", "BIOS & firmware", "Paths & saves"]
