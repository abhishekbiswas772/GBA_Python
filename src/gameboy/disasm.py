from src.gameboy.common import (
    R8, R16, R16_STACK, CONDITIONS as CC, 
    ALU, ROT, ACC_OPS, INDIRECT,
    signed8
)

def disassemble(mmu, addr: int) -> tuple[str, int]:
    rd = lambda i: mmu.read((addr + i) & 0xFFFF)
    op = rd(0)
    d8 = rd(1)
    d16 = rd(1) | (rd(2) << 8)
    x, y, z = op >> 6, (op >> 3) & 7, op & 7
    p, q = y >> 1, y & 1

    def h8(v): return f"0x{v:02X}"
    def h16(v): return f"0x{v:04X}"
    def jr_target(): return h16((addr + 2 + signed8(d8)) & 0xFFFF)

    if op == 0xCB:
        cb = d8
        cx, cy, cz = cb >> 6, (cb >> 3) & 7, cb & 7
        if cx == 0:
            return f"{ROT[cy]} {R8[cz]}", 2
        return f"{['', 'BIT', 'RES', 'SET'][cx]} {cy},{R8[cz]}", 2

    if x == 1:
        return ("HALT", 1) if op == 0x76 else (f"LD {R8[y]},{R8[z]}", 1)
    if x == 2:
        return f"{ALU[y]}{R8[z]}", 1

    if x == 0:
        if z == 0:
            if y == 0: return "NOP", 1
            if y == 1: return f"LD ({h16(d16)}),SP", 3
            if y == 2: return "STOP", 2
            if y == 3: return f"JR {jr_target()}", 2
            return f"JR {CC[y - 4]},{jr_target()}", 2
        if z == 1:
            return (f"LD {R16[p]},{h16(d16)}", 3) if q == 0 else (f"ADD HL,{R16[p]}", 1)
        if z == 2:
            return (f"LD {INDIRECT[p]},A", 1) if q == 0 else (f"LD A,{INDIRECT[p]}", 1)
        if z == 3:
            return f"{'INC' if q == 0 else 'DEC'} {R16[p]}", 1
        if z == 4: return f"INC {R8[y]}", 1
        if z == 5: return f"DEC {R8[y]}", 1
        if z == 6: return f"LD {R8[y]},{h8(d8)}", 2
        return ACC_OPS[y], 1

    if z == 0:
        if y < 4: return f"RET {CC[y]}", 1
        return [(f"LDH ({h8(d8)}),A", 2), (f"ADD SP,{signed8(d8)}", 2),
                (f"LDH A,({h8(d8)})", 2), (f"LD HL,SP{signed8(d8):+d}", 2)][y - 4]
    if z == 1:
        if q == 0: return f"POP {R16_STACK[p]}", 1
        return [("RET", 1), ("RETI", 1), ("JP HL", 1), ("LD SP,HL", 1)][p]
    if z == 2:
        if y < 4: return f"JP {CC[y]},{h16(d16)}", 3
        return [("LD (0xFF00+C),A", 1), (f"LD ({h16(d16)}),A", 3),
                ("LD A,(0xFF00+C)", 1), (f"LD A,({h16(d16)})", 3)][y - 4]
    if z == 3:
        if y == 0: return f"JP {h16(d16)}", 3
        if y == 6: return "DI", 1
        if y == 7: return "EI", 1
    if z == 4 and y < 4:
        return f"CALL {CC[y]},{h16(d16)}", 3
    if z == 5:
        if q == 0: return f"PUSH {R16_STACK[p]}", 1
        if p == 0: return f"CALL {h16(d16)}", 3
    if z == 6:
        return f"{ALU[y]}{h8(d8)}", 2
    if z == 7:
        return f"RST {h8(y * 8)}", 1
    return f"ILLEGAL {h8(op)}", 1


def disassemble_range(mmu, start: int, count: int) -> list[str]:
    lines, addr = [], start
    for _ in range(count):
        text, length = disassemble(mmu, addr)
        raw = " ".join(f"{mmu.read((addr + i) & 0xFFFF):02X}" for i in range(length))
        lines.append(f"0x{addr:04X}: {raw:<9} {text}")
        addr = (addr + length) & 0xFFFF
    return lines
