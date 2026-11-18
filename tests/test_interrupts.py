from tests.helpers import build, make_rom


def test_dispatch_vector_cycles_and_bookkeeping():
    cpu, mmu, _ = build(make_rom(b"\x00" * 8))
    mmu.ie = 0x04
    mmu.request_interrupt(0x04)
    cpu.ime = True
    cpu.pc = 0x150
    cycles = cpu.step()
    assert cycles == 20
    assert cpu.pc == 0x50
    assert cpu.ime is False
    assert mmu.io[0x0F] & 0x04 == 0
    assert cpu.sp == 0xFFFC
    assert mmu.read_word(0xFFFC) == 0x0150


def test_dispatch_all_vectors():
    for bit, vector in ((0x01, 0x40), (0x02, 0x48), (0x04, 0x50), (0x08, 0x58), (0x10, 0x60)):
        cpu, mmu, _ = build(make_rom(b"\x00" * 8))
        mmu.ie = bit
        mmu.request_interrupt(bit)
        cpu.ime = True
        cpu.pc = 0x150
        assert cpu.step() == 20
        assert cpu.pc == vector


def test_priority_lowest_bit_first():
    cpu, mmu, _ = build(make_rom(b"\x00" * 8))
    mmu.ie = 0x1F
    mmu.io[0x0F] = 0x14                 # bits 2 and 4 pending
    cpu.ime = True
    cpu.pc = 0x150
    cpu.step()
    assert cpu.pc == 0x50
    assert mmu.io[0x0F] == 0x10


def test_ie_mask():
    cpu, mmu, _ = build(make_rom(b"\x00" * 8))
    mmu.ie = 0x00
    mmu.io[0x0F] = 0x1F
    cpu.ime = True
    cpu.pc = 0x150
    cycles = cpu.step()
    assert cpu.pc == 0x151             # nothing dispatched
    assert cycles == 4


def test_ei_delay():
    cpu, mmu, _ = build(make_rom(b"\xFB\x00\x00\x00"))
    mmu.ie = 0x04
    mmu.request_interrupt(0x04)
    cpu.pc = 0x150
    cpu.step()                          # EI
    assert cpu.ime is False
    cpu.step()                          # NOP runs, IME on afterwards
    assert cpu.pc == 0x152
    assert cpu.ime is True
    assert cpu.step() == 20             # dispatch only now
    assert cpu.pc == 0x50


def test_di_cancels_ei():
    cpu, mmu, _ = build(make_rom(b"\xFB\xF3\x00\x00"))
    mmu.ie = 0x04
    mmu.request_interrupt(0x04)
    cpu.pc = 0x150
    cpu.step()                          # EI
    cpu.step()                          # DI
    assert cpu.ime is False
    cpu.step()                          # NOP, no dispatch
    assert cpu.pc == 0x153
    assert cpu.ime is False


def test_reti_reenables_ime():
    cpu, mmu, _ = build(make_rom(b"\x00" * 8, patches={0x50: b"\xD9"}))
    mmu.ie = 0x04
    mmu.request_interrupt(0x04)
    cpu.ime = True
    cpu.pc = 0x150
    cpu.step()                          # dispatch
    assert cpu.pc == 0x50
    cpu.step()                          # RETI
    assert cpu.pc == 0x150
    assert cpu.ime is True
    mmu.request_interrupt(0x04)
    cpu.step()                          # dispatch again
    assert cpu.pc == 0x50
    assert mmu.read_word(0xFFFC) == 0x0150


def test_halt_wake_with_ime_off_no_dispatch():
    cpu, mmu, _ = build(make_rom(b"\x76\x04\x00"))
    mmu.ie = 0x04
    cpu.pc = 0x150
    cpu.step()                          # HALT
    assert cpu.halted is True
    assert cpu.step() == 4              # still halted, timer keeps ticking
    assert cpu.halted is True
    mmu.request_interrupt(0x04)
    cpu.step()                          # wake, continue without dispatch
    assert cpu.halted is False
    assert cpu.pc == 0x152
    assert cpu.b == 1


def test_halt_wake_with_ime_on_dispatches():
    cpu, mmu, _ = build(make_rom(b"\x76\x00", patches={0x50: b"\xD9"}))
    mmu.ie = 0x04
    cpu.ime = True
    cpu.pc = 0x150
    cpu.step()                          # HALT
    assert cpu.halted is True
    mmu.request_interrupt(0x04)
    assert cpu.step() == 20             # wake + dispatch
    assert cpu.pc == 0x50


def test_halt_bug_double_fetch():
    cpu, mmu, _ = build(make_rom(b"\x76\x04\x04\x04"))
    mmu.ie = 0x04
    mmu.request_interrupt(0x04)         # pending with IME=0 at HALT
    cpu.pc = 0x150
    cpu.step()                          # HALT: fails to halt, sets halt_bug
    assert cpu.halted is False
    assert cpu.halt_bug is True
    cpu.step()                          # fetch of 0x04 without PC increment
    assert cpu.pc == 0x151
    assert cpu.b == 1
    cpu.step()
    assert cpu.pc == 0x152
    assert cpu.b == 2
    cpu.step()
    assert cpu.pc == 0x153
    assert cpu.b == 3                   # byte after HALT executed twice


def test_halt_bug_with_ei_does_not_trigger():
    cpu, mmu, _ = build(make_rom(b"\xFB\x76\x00", patches={0x50: b"\xD9"}))
    mmu.ie = 0x04
    mmu.request_interrupt(0x04)
    cpu.pc = 0x150
    cpu.step()                          # EI
    cpu.step()                          # HALT: ime still off but pending EI
    assert cpu.halted is True
    assert cpu.halt_bug is False
    assert cpu.step() == 20             # wake + dispatch
    assert cpu.pc == 0x50


def test_timer_overflow_triggers_dispatch():
    cpu, mmu, timer = build(make_rom(b"\x00" * 8, patches={0x50: b"\xD9"}))
    mmu.ie = 0x04
    timer.write(0xFF06, 0x00)
    timer.write(0xFF05, 0xFF)
    timer.write(0xFF07, 0x05)
    cpu.ime = True
    cpu.pc = 0x150
    timer.tick(16)
    assert mmu.io[0x0F] & 0x04
    assert cpu.step() == 20
    assert cpu.pc == 0x50


def test_io_read_masks():
    from src.core.gameboy.cartridge import Cartridge
    from src.core.gameboy.mmu import MMU
    from tests.helpers import blank_rom
    mmu = MMU(Cartridge(bytes(blank_rom())))
    mmu.write(0xFF0F, 0x00)
    assert mmu.read(0xFF0F) == 0xE0      # IF upper bits read 1
    mmu.write(0xFF02, 0x00)
    assert mmu.read(0xFF02) == 0x7E
    for addr in (0xFF03, 0xFF4D, 0xFF50, 0xFF7F):
        mmu.write(addr, 0x12)
        assert mmu.read(addr) == 0xFF    # unmapped


def test_serial_transfer_completes_without_partner():
    from src.core.gameboy.cartridge import Cartridge
    from src.core.gameboy.mmu import MMU
    from tests.helpers import blank_rom
    mmu = MMU(Cartridge(bytes(blank_rom())))
    mmu.write(0xFF01, ord("A"))
    mmu.write(0xFF02, 0x81)
    assert mmu.serial_text() == "A"
    assert mmu.read(0xFF01) == 0xFF
    assert mmu.read(0xFF02) & 0x80 == 0
    assert mmu.io[0x0F] & 0x08


def test_debugger_steps_and_breaks():
    from src.core.gameboy.debugger import Debugger

    class _GB:                           # minimal GameBoy facade
        def __init__(self):
            self.cpu, self.mmu, self.timer = build(make_rom(b"\x00\x00\x00\x18\xFE"))

        def steps(self):
            c = self.cpu.step()
            self.timer.tick(c)
            return c

    dbg = Debugger(_GB())
    dbg.breakpoints.add(0x153)
    assert dbg.run(100) == "breakpoint at 0x0153"
    assert list(dbg.history) == [0x150, 0x151, 0x152]
    assert "0x0150" in dbg.show_history()
    assert "commands" in Debugger.HELP
