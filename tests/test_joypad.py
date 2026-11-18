import os

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")

import pygame

from src.gameboy.display import KEYMAP, joypad_key_handler
from src.gameboy.gameboy import GameBoy
from src.gameboy.joypad import DOWN, RIGHT, START, A
from tests.helpers import build, make_rom


def make_joypad():
    cpu, mmu, _ = build(make_rom(b"\x00" * 8))
    return mmu.joypad, mmu


# --- register semantics -----------------------------------------------------

def test_read_reset_state():
    joy, _ = make_joypad()
    assert joy.read() == 0xFF                    # selects off, nibble 0xF


def test_direction_group_selected():
    joy, _ = make_joypad()
    joy.write(0x20)                              # bit4=0: directions selected
    joy.press("right")
    assert joy.read() & 0x0F == 0x0E             # bit0 = Right
    joy.press("down")
    assert joy.read() & 0x0F == 0x06             # bit3 = Down
    joy.press("a")                               # other group ignored
    joy.press("start")
    assert joy.read() & 0x0F == 0x06
    joy.release("right")
    assert joy.read() & 0x0F == 0x07


def test_button_group_selected():
    joy, _ = make_joypad()
    joy.write(0x10)                              # bit5=0: buttons selected
    joy.press("a")
    assert joy.read() & 0x0F == 0x0E             # bit0 = A
    joy.press("start")
    assert joy.read() & 0x0F == 0x06             # bit3 = Start
    joy.press("left")                            # other group ignored
    joy.press("up")
    assert joy.read() & 0x0F == 0x06
    joy.release("a")
    assert joy.read() & 0x0F == 0x07


def test_both_groups_selected_ands():
    joy, _ = make_joypad()
    joy.write(0x00)
    joy.press("a")                               # bit0 of buttons
    joy.press("left")                            # bit1 of directions
    assert joy.read() & 0x0F == 0x0C
    joy.press("b")                               # bit1 of buttons too
    joy.press("right")                           # bit0 of directions too
    assert joy.read() & 0x0F == 0x0C


def test_no_group_selected_reads_f():
    joy, _ = make_joypad()
    joy.write(0x30)
    for b in ("a", "b", "select", "start", "right", "left", "up", "down"):
        joy.press(b)
    assert joy.read() & 0x0F == 0x0F
    assert joy.read() == 0xFF


def test_write_sets_selects_only():
    joy, _ = make_joypad()
    joy.write(0x10)
    assert joy.read() & 0x30 == 0x10
    joy.write(0x20)
    assert joy.read() & 0x30 == 0x20
    assert joy.read() & 0xC0 == 0xC0             # bits 7-6 read as 1
    joy.press("a")                               # buttons are external
    assert joy.read() & 0x30 == 0x20             # presses do not touch selects
    joy.write(0x0F)                              # low nibble write ignored
    assert joy.read() & 0x0F == 0x0E             # written bits press nothing
    joy.release("a")
    assert joy.read() & 0x0F == 0x0F


def test_press_release_roundtrip():
    joy, _ = make_joypad()
    joy.write(0x20)
    joy.press("up")
    joy.press("down")
    assert joy.read() & 0x0F == 0x03
    joy.release("up")
    joy.release("down")
    assert joy.read() & 0x0F == 0x0F


def test_press_accepts_int_ids():
    joy, _ = make_joypad()
    joy.write(0x00)
    joy.press(A)
    joy.press(DOWN)
    assert joy.read() & 0x0F == 0x06
    joy.release(A)
    joy.release(DOWN)
    assert joy.read() & 0x0F == 0x0F


# --- interrupt (IF bit 4) ---------------------------------------------------

def test_interrupt_on_press_selected_group():
    joy, mmu = make_joypad()
    joy.write(0x20)                              # directions selected
    mmu.io[0x0F] = 0
    joy.press("right")
    assert mmu.io[0x0F] & 0x10


def test_interrupt_both_groups_selected():
    joy, mmu = make_joypad()
    joy.write(0x00)
    mmu.io[0x0F] = 0
    joy.press("b")
    assert mmu.io[0x0F] & 0x10


def test_no_interrupt_unselected_group():
    joy, mmu = make_joypad()
    joy.write(0x20)                              # directions selected
    mmu.io[0x0F] = 0
    joy.press("a")
    joy.press("start")
    assert mmu.io[0x0F] & 0x10 == 0


def test_no_interrupt_release_or_key_repeat():
    joy, mmu = make_joypad()
    joy.write(0x20)
    joy.press("right")
    mmu.io[0x0F] = 0
    joy.press("right")                           # key repeat: no new 1->0
    assert mmu.io[0x0F] & 0x10 == 0
    joy.release("right")
    assert mmu.io[0x0F] & 0x10 == 0


def test_select_write_revealing_held_key_interrupts():
    joy, mmu = make_joypad()
    joy.write(0x30)                              # nothing selected
    joy.press("a")
    assert mmu.io[0x0F] & 0x10 == 0              # unselected: no falling edge
    mmu.io[0x0F] = 0
    joy.write(0x10)                              # select buttons: line falls
    assert mmu.io[0x0F] & 0x10


# --- MMU wiring -------------------------------------------------------------

def test_mmu_routes_ff00():
    joy, mmu = make_joypad()
    mmu.write(0xFF00, 0x20)
    assert mmu.read(0xFF00) == 0xEF              # 0xC0 | selects | 0x0F
    mmu.joypad.press("left")
    assert mmu.read(0xFF00) & 0x0F == 0x0D
    mmu.io[0x0F] = 0
    mmu.joypad.press(RIGHT)
    assert mmu.io[0x0F] & 0x10                   # request_interrupt(0x10)
    mmu.joypad.release("left")
    mmu.joypad.release(RIGHT)
    assert mmu.read(0xFF00) & 0x0F == 0x0F


# --- CPU integration with hand-assembled ROMs -------------------------------

POLL_ROM = bytes([
    0x3E, 0x00,          # LD A,0x00
    0xE0, 0x00,          # LDH (0xFF00),A       both groups selected
    0xF0, 0x00,          # LDH A,(0xFF00)
    0xE6, 0x0F,          # AND 0x0F
    0xEA, 0x00, 0xC0,    # LD (0xC000),A
    0x18, 0xF7,          # JR back to the read
])

INT_MAIN = bytes([
    0x3E, 0x00,          # LD A,0x00
    0xE0, 0x00,          # LDH (0xFF00),A       both groups selected
    0x3E, 0x10,          # LD A,0x10
    0xE0, 0xFF,          # LDH (0xFFFF),A       IE = joypad
    0xFB,                # EI
    0x76,                # HALT
    0x18, 0xFD,          # JR back to the HALT
])

INT_ISR = bytes([
    0xF0, 0x00,          # LDH A,(0xFF00)
    0xE0, 0x80,          # LDH (0xFF80),A       keep raw P1 value
    0x2F,                # CPL
    0xE6, 0x0F,          # AND 0x0F             pressed mask
    0xC6, 0x30,          # ADD A,0x30           ASCII digit
    0xE0, 0x01,          # LDH (0xFF01),A       SB
    0x3E, 0x81,          # LD A,0x81
    0xE0, 0x02,          # LDH (0xFF02),A       SC -> serial out
    0xD9,                # RETI
])


def test_cpu_polling_rom_observes_presses(tmp_path):
    rom = tmp_path / "poll.gb"
    rom.write_bytes(make_rom(POLL_ROM))
    gb = GameBoy(rom)
    for _ in range(500):
        gb.steps()
    assert gb.mmu.read(0xC000) == 0x0F           # nothing pressed

    gb.joypad.press("a")
    for _ in range(500):
        gb.steps()
    assert gb.mmu.read(0xC000) == 0x0E

    gb.joypad.release("a")
    gb.joypad.press("down")
    for _ in range(500):
        gb.steps()
    assert gb.mmu.read(0xC000) == 0x07
    assert gb.mmu.serial_text() == ""


def test_cpu_joypad_interrupt_rom(tmp_path):
    rom = tmp_path / "joy.gb"
    rom.write_bytes(make_rom(INT_MAIN, patches={0x60: INT_ISR}))
    gb = GameBoy(rom)
    for _ in range(500):
        gb.steps()
    assert gb.cpu.halted                        # sleeping until joypad IRQ
    assert gb.mmu.serial_text() == ""

    gb.joypad.press("a")                        # wakes CPU, dispatches 0x60
    for _ in range(500):
        gb.steps()
    assert gb.mmu.read(0xFF80) == 0xCE          # raw P1 seen by the ISR
    assert gb.mmu.serial_text() == "1"

    gb.joypad.release("a")
    gb.joypad.press(START)
    for _ in range(500):
        gb.steps()
    assert gb.mmu.read(0xFF80) == 0xC7
    assert gb.mmu.serial_text() == "18"


# --- keyboard events (dummy SDL video driver, no real window) ---------------

def test_key_events_route_to_joypad():
    joy, mmu = make_joypad()
    on_key = joypad_key_handler(joy)
    assert KEYMAP[pygame.K_x] == "a" and KEYMAP[pygame.K_RETURN] == "start"
    mmu.write(0xFF00, 0x20)                      # directions selected

    mmu.io[0x0F] = 0
    on_key(pygame.event.Event(pygame.KEYDOWN, key=pygame.K_RIGHT, repeat=False))
    assert mmu.io[0x0F] & 0x10
    assert mmu.read(0xFF00) & 0x0F == 0x0E

    mmu.io[0x0F] = 0
    on_key(pygame.event.Event(pygame.KEYDOWN, key=pygame.K_RIGHT, repeat=True))
    assert mmu.io[0x0F] & 0x10 == 0             # key repeat ignored

    on_key(pygame.event.Event(pygame.KEYUP, key=pygame.K_RIGHT))
    assert mmu.read(0xFF00) & 0x0F == 0x0F

    on_key(pygame.event.Event(pygame.KEYDOWN, key=pygame.K_q, repeat=False))
    assert joy.buttons == 0 and joy.dirs == 0    # unmapped key ignored
