from tests.helpers import build, make_rom


def test_add_imm_flags():
    cpu, mmu, _ = build(make_rom(b"\x3E\x3A\xC6\x06"))
    cpu.step()
    assert cpu.a == 0x3A
    cpu.step()
    assert cpu.a == 0x40
    assert cpu.f == 0x20                 # H set, Z/N/C clear


def test_call_ret_roundtrip():
    cpu, mmu, _ = build(make_rom(b"\xCD\x60\x01\x00", patches={0x160: b"\xC9"}))
    cpu.step()
    assert cpu.pc == 0x160
    assert cpu.sp == 0xFFFC
    cpu.step()
    assert cpu.pc == 0x153
    assert cpu.sp == 0xFFFE


def test_ld_hl_memory():
    cpu, mmu, _ = build(make_rom(b"\x21\x00\xC0\x36\x55\x7E"))
    cpu.step()
    cpu.step()
    assert mmu.wram[0] == 0x55
    cpu.step()
    assert cpu.a == 0x55


def test_jr_relative():
    cpu, mmu, _ = build(make_rom(b"\x18\x02\x00\x00\x3E\x77"))
    cpu.step()
    assert cpu.pc == 0x154
    cpu.step()
    assert cpu.a == 0x77


def test_push_pop_af_masks_low_nibble():
    cpu, mmu, _ = build(make_rom(b"\xF5\xF1"))
    cpu.f = 0xFF
    cpu.step()
    cpu.f = 0x00
    cpu.step()
    assert cpu.f == 0xF0
    assert cpu.sp == 0xFFFE
