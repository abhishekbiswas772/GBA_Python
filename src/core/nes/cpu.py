"""Ricoh 2A03 CPU (MOS 6502 without decimal mode).

Speed matters most here, so the interpreter loop is generated once at import:
one function in which every register and flag is a local variable, opcodes
are dispatched through a binary if-tree (8 integer compares, no calls), and
memory access is inlined with fast paths for internal RAM and PRG ROM.
Only I/O ($2000-$7FFF) goes through the bus callbacks ``rd`` / ``wr``.

State lives in attributes between ``run()`` calls:
``a x y s pc`` registers, ``c v i d`` flags as 0/1, and the N / Z flags kept
lazily as ``nr`` (bit 7 = N) and ``zr`` (zero = Z set).

Interrupts: the bus sets ``nmi_pending`` / ``irq_line``. Any I/O write
that needs the loop to stop early (NMI enabled during vblank, OAM DMA stall,
IRQ change) sets ``signal``; the loop finishes the instruction and returns, and
``run()`` services interrupts between instructions.

All official opcodes plus the stable unofficial ones (NOPs, LAX, SAX, DCP,
ISB, SLO, RLA, SRE, RRA, ANC, ALR, ARR, AXS, SBC $EB) are implemented, and
the unstable SHx / TAS / LAS / XAA family approximately. KIL jams the CPU.
"""
from __future__ import annotations

# -------------------------------------------------------------------- opcode table
# mode: imp acc imm zp zpx zpy abs abx aby ind izx izy rel
_LEN = {"imp": 1, "acc": 1, "imm": 2, "zp": 2, "zpx": 2, "zpy": 2, "abs": 3, "abx": 3, "aby": 3,
        "ind": 3, "izx": 2, "izy": 2, "rel": 2}

_TABLE = """
00 BRK imp 7|01 ORA izx 6|02 KIL imp 2|03 SLO izx 8|04 NOP zp 3|05 ORA zp 3|06 ASL zp 5|07 SLO zp 5
08 PHP imp 3|09 ORA imm 2|0A ASL acc 2|0B ANC imm 2|0C NOP abs 4|0D ORA abs 4|0E ASL abs 6|0F SLO abs 6
10 BPL rel 2|11 ORA izy 5*|12 KIL imp 2|13 SLO izy 8|14 NOP zpx 4|15 ORA zpx 4|16 ASL zpx 6|17 SLO zpx 6
18 CLC imp 2|19 ORA aby 4*|1A NOP imp 2|1B SLO aby 7|1C NOP abx 4*|1D ORA abx 4*|1E ASL abx 7|1F SLO abx 7
20 JSR abs 6|21 AND izx 6|22 KIL imp 2|23 RLA izx 8|24 BIT zp 3|25 AND zp 3|26 ROL zp 5|27 RLA zp 5
28 PLP imp 4|29 AND imm 2|2A ROL acc 2|2B ANC imm 2|2C BIT abs 4|2D AND abs 4|2E ROL abs 6|2F RLA abs 6
30 BMI rel 2|31 AND izy 5*|32 KIL imp 2|33 RLA izy 8|34 NOP zpx 4|35 AND zpx 4|36 ROL zpx 6|37 RLA zpx 6
38 SEC imp 2|39 AND aby 4*|3A NOP imp 2|3B RLA aby 7|3C NOP abx 4*|3D AND abx 4*|3E ROL abx 7|3F RLA abx 7
40 RTI imp 6|41 EOR izx 6|42 KIL imp 2|43 SRE izx 8|44 NOP zp 3|45 EOR zp 3|46 LSR zp 5|47 SRE zp 5
48 PHA imp 3|49 EOR imm 2|4A LSR acc 2|4B ALR imm 2|4C JMP abs 3|4D EOR abs 4|4E LSR abs 6|4F SRE abs 6
50 BVC rel 2|51 EOR izy 5*|52 KIL imp 2|53 SRE izy 8|54 NOP zpx 4|55 EOR zpx 4|56 LSR zpx 6|57 SRE zpx 6
58 CLI imp 2|59 EOR aby 4*|5A NOP imp 2|5B SRE aby 7|5C NOP abx 4*|5D EOR abx 4*|5E LSR abx 7|5F SRE abx 7
60 RTS imp 6|61 ADC izx 6|62 KIL imp 2|63 RRA izx 8|64 NOP zp 3|65 ADC zp 3|66 ROR zp 5|67 RRA zp 5
68 PLA imp 4|69 ADC imm 2|6A ROR acc 2|6B ARR imm 2|6C JMP ind 5|6D ADC abs 4|6E ROR abs 6|6F RRA abs 6
70 BVS rel 2|71 ADC izy 5*|72 KIL imp 2|73 RRA izy 8|74 NOP zpx 4|75 ADC zpx 4|76 ROR zpx 6|77 RRA zpx 6
78 SEI imp 2|79 ADC aby 4*|7A NOP imp 2|7B RRA aby 7|7C NOP abx 4*|7D ADC abx 4*|7E ROR abx 7|7F RRA abx 7
80 NOP imm 2|81 STA izx 6|82 NOP imm 2|83 SAX izx 6|84 STY zp 3|85 STA zp 3|86 STX zp 3|87 SAX zp 3
88 DEY imp 2|89 NOP imm 2|8A TXA imp 2|8B XAA imm 2|8C STY abs 4|8D STA abs 4|8E STX abs 4|8F SAX abs 4
90 BCC rel 2|91 STA izy 6|92 KIL imp 2|93 AHX izy 6|94 STY zpx 4|95 STA zpx 4|96 STX zpy 4|97 SAX zpy 4
98 TYA imp 2|99 STA aby 5|9A TXS imp 2|9B TAS aby 5|9C SHY abx 5|9D STA abx 5|9E SHX aby 5|9F AHX aby 5
A0 LDY imm 2|A1 LDA izx 6|A2 LDX imm 2|A3 LAX izx 6|A4 LDY zp 3|A5 LDA zp 3|A6 LDX zp 3|A7 LAX zp 3
A8 TAY imp 2|A9 LDA imm 2|AA TAX imp 2|AB LAX imm 2|AC LDY abs 4|AD LDA abs 4|AE LDX abs 4|AF LAX abs 4
B0 BCS rel 2|B1 LDA izy 5*|B2 KIL imp 2|B3 LAX izy 5*|B4 LDY zpx 4|B5 LDA zpx 4|B6 LDX zpy 4|B7 LAX zpy 4
B8 CLV imp 2|B9 LDA aby 4*|BA TSX imp 2|BB LAS aby 4*|BC LDY abx 4*|BD LDA abx 4*|BE LDX aby 4*|BF LAX aby 4*
C0 CPY imm 2|C1 CMP izx 6|C2 NOP imm 2|C3 DCP izx 8|C4 CPY zp 3|C5 CMP zp 3|C6 DEC zp 5|C7 DCP zp 5
C8 INY imp 2|C9 CMP imm 2|CA DEX imp 2|CB AXS imm 2|CC CPY abs 4|CD CMP abs 4|CE DEC abs 6|CF DCP abs 6
D0 BNE rel 2|D1 CMP izy 5*|D2 KIL imp 2|D3 DCP izy 8|D4 NOP zpx 4|D5 CMP zpx 4|D6 DEC zpx 6|D7 DCP zpx 6
D8 CLD imp 2|D9 CMP aby 4*|DA NOP imp 2|DB DCP aby 7|DC NOP abx 4*|DD CMP abx 4*|DE DEC abx 7|DF DCP abx 7
E0 CPX imm 2|E1 SBC izx 6|E2 NOP imm 2|E3 ISB izx 8|E4 CPX zp 3|E5 SBC zp 3|E6 INC zp 5|E7 ISB zp 5
E8 INX imp 2|E9 SBC imm 2|EA NOP imp 2|EB SBC imm 2|EC CPX abs 4|ED SBC abs 4|EE INC abs 6|EF ISB abs 6
F0 BEQ rel 2|F1 SBC izy 5*|F2 KIL imp 2|F3 ISB izy 8|F4 NOP zpx 4|F5 SBC zpx 4|F6 INC zpx 6|F7 ISB zpx 6
F8 SED imp 2|F9 SBC aby 4*|FA NOP imp 2|FB ISB aby 7|FC NOP abx 4*|FD SBC abx 4*|FE INC abx 7|FF ISB abx 7
"""


def _parse():
    ops = {}
    for row in _TABLE.strip().splitlines():
        for cell in row.split("|"):
            code, name, mode, cyc = cell.split()
            ops[int(code, 16)] = (name, mode, int(cyc.rstrip("*")), cyc.endswith("*"))
    assert len(ops) == 256
    return ops


OPS = _parse()
OFFICIAL = {
    "ADC", "AND", "ASL", "BCC", "BCS", "BEQ", "BIT", "BMI", "BNE", "BPL", "BRK", "BVC", "BVS", "CLC",
    "CLD", "CLI", "CLV", "CMP", "CPX", "CPY", "DEC", "DEX", "DEY", "EOR", "INC", "INX", "INY", "JMP",
    "JSR", "LDA", "LDX", "LDY", "LSR", "NOP", "ORA", "PHA", "PHP", "PLA", "PLP", "ROL", "ROR", "RTI",
    "RTS", "SBC", "SEC", "SED", "SEI", "STA", "STX", "STY", "TAX", "TAY", "TSX", "TXA", "TXS", "TYA",
}

_P = "((nr & 0x80) | (v << 6) | 0x20 | (d << 3) | (i << 2) | (0 if zr else 2) | c)"
_SETP = ["nr = p & 0x80", "v = (p >> 6) & 1", "d = (p >> 3) & 1", "i = (p >> 2) & 1",
         "zr = 0 if p & 2 else 1", "c = p & 1"]

_READ_OPS = {"LDA", "LDX", "LDY", "ADC", "SBC", "AND", "ORA", "EOR", "CMP", "CPX", "CPY", "BIT", "LAX",
             "NOP", "ANC", "ALR", "ARR", "AXS", "LAS", "XAA"}
_WRITE_OPS = {"STA", "STX", "STY", "SAX", "SHX", "SHY", "AHX", "TAS"}
_RMW_OPS = {"ASL", "LSR", "ROL", "ROR", "INC", "DEC", "SLO", "RLA", "SRE", "RRA", "DCP", "ISB"}


def _read(dst: str, ea: str, k: int) -> list[str]:
    return [f"if {ea} < 0x2000: {dst} = ram[{ea} & 0x7FF]",
            f"elif {ea} >= 0x8000: {dst} = prg[pb[({ea} >> 13) & 3] + ({ea} & 0x1FFF)]",
            f"else:",
            f"    cpu.cyc = cyc + {k}",
            f"    {dst} = rd({ea})"]


def _write(ea: str, val: str, k: int) -> list[str]:
    return [f"if {ea} < 0x2000: ram[{ea} & 0x7FF] = {val}",
            f"else:",
            f"    cpu.cyc = cyc + {k}",
            f"    wr({ea}, {val})",
            f"    if cpu.signal:",
            f"        cpu.signal = False",
            f"        cyc += cpu.stall",
            f"        cpu.stall = 0",
            f"        limit = cyc"]


def _ea(mode: str, penalty: bool) -> tuple[list[str], bool]:
    """Code computing `ea`; returns (lines, zero_page)."""
    if mode == "zp":
        return ["ea = b1"], True
    if mode == "zpx":
        return ["ea = (b1 + x) & 0xFF"], True
    if mode == "zpy":
        return ["ea = (b1 + y) & 0xFF"], True
    if mode == "abs":
        return ["ea = b1 | (b2 << 8)"], False
    if mode in ("abx", "aby"):
        r = "x" if mode == "abx" else "y"
        lines = [f"ea = (b1 | (b2 << 8)) + {r}"]
        if penalty:
            lines.append("if b1 + " + r + " > 0xFF: cyc += 1")
        lines.append("ea &= 0xFFFF")
        return lines, False
    if mode == "izx":
        return ["zp = (b1 + x) & 0xFF", "ea = ram[zp] | (ram[(zp + 1) & 0xFF] << 8)"], False
    if mode == "izy":
        lines = ["lo = ram[b1]", "ea = (lo | (ram[(b1 + 1) & 0xFF] << 8)) + y"]
        if penalty:
            lines.append("if lo + y > 0xFF: cyc += 1")
        lines.append("ea &= 0xFFFF")
        return lines, False
    raise ValueError(mode)


def _load(mode: str, penalty: bool, k: int) -> list[str]:
    """Code leaving the operand value in `t`."""
    if mode == "imm":
        return ["t = b1"]
    if mode == "imp":                                # NOP
        return []
    lines, zp = _ea(mode, penalty)
    return lines + (["t = ram[ea]"] if zp else _read("t", "ea", k))


def _store(mode: str, val: str, k: int) -> list[str]:
    lines, zp = _ea(mode, False)
    return lines + ([f"ram[ea] = {val}"] if zp else _write("ea", val, k))


_NZ = "zr = nr = "


def _alu(name: str) -> list[str]:
    """Read-op bodies operating on `t`."""
    return {
        "LDA": ["a = t", _NZ + "a"],
        "LDX": ["x = t", _NZ + "x"],
        "LDY": ["y = t", _NZ + "y"],
        "LAX": ["a = x = t", _NZ + "a"],
        "AND": ["a &= t", _NZ + "a"],
        "ORA": ["a |= t", _NZ + "a"],
        "EOR": ["a ^= t", _NZ + "a"],
        "ADC": ["r = a + t + c", "v = ((~(a ^ t) & (a ^ r)) >> 7) & 1", "c = r >> 8", "a = r & 0xFF", _NZ + "a"],
        "SBC": ["t ^= 0xFF", "r = a + t + c", "v = ((~(a ^ t) & (a ^ r)) >> 7) & 1", "c = r >> 8",
                "a = r & 0xFF", _NZ + "a"],
        "CMP": ["r = a - t", "c = 1 if r >= 0 else 0", _NZ + "r & 0xFF"],
        "CPX": ["r = x - t", "c = 1 if r >= 0 else 0", _NZ + "r & 0xFF"],
        "CPY": ["r = y - t", "c = 1 if r >= 0 else 0", _NZ + "r & 0xFF"],
        "BIT": ["zr = a & t", "nr = t", "v = (t >> 6) & 1"],
        "NOP": [],
        "ANC": ["a &= t", _NZ + "a", "c = a >> 7"],
        "ALR": ["a &= t", "c = a & 1", "a >>= 1", _NZ + "a"],
        "ARR": ["a = ((a & t) >> 1) | (c << 7)", _NZ + "a", "c = (a >> 6) & 1", "v = ((a >> 6) ^ (a >> 5)) & 1"],
        "AXS": ["r = (a & x) - t", "c = 1 if r >= 0 else 0", "x = r & 0xFF", _NZ + "x"],
        "LAS": ["a = x = s = t & s", _NZ + "a"],
        "XAA": ["a = x & t", _NZ + "a"],
    }[name]


def _shift(name: str, var: str) -> list[str]:
    return {
        "ASL": [f"c = {var} >> 7", f"{var} = ({var} << 1) & 0xFF"],
        "LSR": [f"c = {var} & 1", f"{var} >>= 1"],
        "ROL": [f"r = ({var} << 1) | c", "c = r >> 8", f"{var} = r & 0xFF"],
        "ROR": [f"r = ({var} >> 1) | (c << 7)", f"c = {var} & 1", f"{var} = r"],
        "INC": [f"{var} = ({var} + 1) & 0xFF"],
        "DEC": [f"{var} = ({var} - 1) & 0xFF"],
    }[name]


_COMBO = {"SLO": ("ASL", "ORA"), "RLA": ("ROL", "AND"), "SRE": ("LSR", "EOR"), "RRA": ("ROR", "ADC"),
          "DCP": ("DEC", "CMP"), "ISB": ("INC", "SBC")}

_BRANCH = {"BPL": "not nr & 0x80", "BMI": "nr & 0x80", "BVC": "not v", "BVS": "v",
           "BCC": "not c", "BCS": "c", "BNE": "zr", "BEQ": "not zr"}


def _body(op: int) -> list[str]:
    name, mode, cyc, penalty = OPS[op]
    k = cyc - 1                                     # bus access happens on the last cycle(s)
    L = [f"pc = (pc + {_LEN[mode]}) & 0xFFFF"]
    if name in _BRANCH:
        L += [f"if {_BRANCH[name]}:",
              "    t = (pc + (b1 - 256 if b1 & 0x80 else b1)) & 0xFFFF",
              "    cyc += 2 if (t ^ pc) & 0xFF00 else 1",
              "    pc = t"]
    elif name in _READ_OPS:
        L += _load(mode, penalty, k) + _alu(name)
    elif name in _WRITE_OPS:
        val = {"STA": "a", "STX": "x", "STY": "y", "SAX": "a & x"}.get(name)
        if val is not None:
            L += _store(mode, val, k)
        else:                                        # SHX / SHY / AHX / TAS: value & (high byte + 1)
            src = {"SHX": "x", "SHY": "y", "AHX": "a & x", "TAS": "a & x"}[name]
            if name == "TAS":
                L.append("s = a & x")
            if mode == "izy":
                L += ["hi = ram[(b1 + 1) & 0xFF]", "base = ram[b1] | (hi << 8)"]
                idx = "y"
            else:
                L += ["hi = b2", "base = b1 | (b2 << 8)"]
                idx = "x" if mode == "abx" else "y"
            L += [f"ea = (base + {idx}) & 0xFFFF",
                  f"t = ({src}) & ((hi + 1) & 0xFF)",
                  "if (base ^ ea) & 0x100: ea = (t << 8) | (ea & 0xFF)"]   # page cross: high byte = value
            L += _write("ea", "t", k)
    elif name in _RMW_OPS:
        if mode == "acc":
            L += _shift(name, "a") + [_NZ + "a"]
        else:
            lines, zp = _ea(mode, False)
            L += lines + (["t = ram[ea]"] if zp else _read("t", "ea", k - 1))
            first, second = _COMBO.get(name, (name, None))
            L += _shift(first, "t")
            L += (["ram[ea] = t"] if zp else _write("ea", "t", k))
            L += _alu(second) if second else [_NZ + "t"]
    elif name == "JMP":
        if mode == "abs":
            L.append("pc = b1 | (b2 << 8)")
        else:                                        # indirect, with the page-wrap bug
            L += ["ea = b1 | (b2 << 8)"] + _read("lo", "ea", 3) + \
                 ["ea = (ea & 0xFF00) | ((ea + 1) & 0xFF)"] + _read("hi", "ea", 4) + \
                 ["pc = lo | (hi << 8)"]
    elif name == "JSR":
        L += ["r = (pc - 1) & 0xFFFF",
              "ram[0x100 | s] = r >> 8", "s = (s - 1) & 0xFF",
              "ram[0x100 | s] = r & 0xFF", "s = (s - 1) & 0xFF",
              "pc = b1 | (b2 << 8)"]
    elif name == "RTS":
        L += ["s = (s + 1) & 0xFF", "lo = ram[0x100 | s]", "s = (s + 1) & 0xFF",
              "pc = ((lo | (ram[0x100 | s] << 8)) + 1) & 0xFFFF"]
    elif name == "RTI":
        L += ["s = (s + 1) & 0xFF", "p = ram[0x100 | s]"] + _SETP + \
             ["s = (s + 1) & 0xFF", "lo = ram[0x100 | s]", "s = (s + 1) & 0xFF",
              "pc = lo | (ram[0x100 | s] << 8)",
              "if cpu.irq_line and not i: limit = cyc"]
    elif name == "BRK":
        L += ["r = (pc + 1) & 0xFFFF",
              "ram[0x100 | s] = r >> 8", "s = (s - 1) & 0xFF",
              "ram[0x100 | s] = r & 0xFF", "s = (s - 1) & 0xFF",
              f"ram[0x100 | s] = {_P} | 0x10", "s = (s - 1) & 0xFF",
              "i = 1",
              "pc = cpu.read16(0xFFFA if cpu.nmi_pending else 0xFFFE)",
              "cpu.nmi_pending = False"]
    elif name == "PHA":
        L += ["ram[0x100 | s] = a", "s = (s - 1) & 0xFF"]
    elif name == "PHP":
        L += [f"ram[0x100 | s] = {_P} | 0x10", "s = (s - 1) & 0xFF"]
    elif name == "PLA":
        L += ["s = (s + 1) & 0xFF", "a = ram[0x100 | s]", _NZ + "a"]
    elif name == "PLP":
        L += ["s = (s + 1) & 0xFF", "p = ram[0x100 | s]"] + _SETP + \
             ["if cpu.irq_line and not i: limit = cyc"]
    elif name == "KIL":
        L += ["pc = (pc - 1) & 0xFFFF", "cpu.jammed = True", "limit = cyc"]
    else:
        L += {
            "CLC": ["c = 0"], "SEC": ["c = 1"], "CLV": ["v = 0"],
            "CLD": ["d = 0"], "SED": ["d = 1"], "SEI": ["i = 1"],
            "CLI": ["i = 0", "if cpu.irq_line: limit = cyc"],
            "TAX": ["x = a", _NZ + "x"], "TAY": ["y = a", _NZ + "y"],
            "TXA": ["a = x", _NZ + "a"], "TYA": ["a = y", _NZ + "a"],
            "TSX": ["x = s", _NZ + "x"], "TXS": ["s = x"],
            "INX": ["x = (x + 1) & 0xFF", _NZ + "x"], "INY": ["y = (y + 1) & 0xFF", _NZ + "y"],
            "DEX": ["x = (x - 1) & 0xFF", _NZ + "x"], "DEY": ["y = (y - 1) & 0xFF", _NZ + "y"],
            "NOP": [],
        }[name]
    L.append(f"cyc += {cyc}")
    return L


def _tree(lo: int, hi: int, depth: int) -> list[str]:
    pad = "    " * depth
    if hi - lo == 1:
        return [pad + line for line in _body(lo)]
    mid = (lo + hi) // 2
    return ([f"{pad}if op < {mid}:"] + _tree(lo, mid, depth + 1)
            + [f"{pad}else:"] + _tree(mid, hi, depth + 1))


_STATE = ("a", "x", "y", "s", "pc", "cyc", "c", "v", "i", "d", "zr", "nr")


def _gen_run() -> str:
    L = ["def run_loop(cpu, limit):",
         "    ram = cpu.ram; prg = cpu.prg; pb = cpu.pb; rd = cpu.rd; wr = cpu.wr; peek = cpu.peek"]
    L += [f"    {name} = cpu.{name}" for name in _STATE]
    L += ["    while cyc < limit:",
          "        if pc >= 0x8000 and (pc & 0x1FFF) < 0x1FFE:",
          "            k = pb[(pc >> 13) & 3] + (pc & 0x1FFF)",
          "            op = prg[k]; b1 = prg[k + 1]; b2 = prg[k + 2]",
          "        else:",
          "            op = peek(pc); b1 = peek((pc + 1) & 0xFFFF); b2 = peek((pc + 2) & 0xFFFF)"]
    L += _tree(0, 256, 2)
    L += [f"    cpu.{name} = {name}" for name in _STATE]
    return "\n".join(L) + "\n"


SOURCE = _gen_run()
_ns: dict = {}
exec(compile(SOURCE, "<nes-cpu>", "exec"), _ns)
run_loop = _ns["run_loop"]


class CPU:
    """2A03 core. The owner wires ``ram``, ``prg`` / ``pb`` (mapper banks),
    ``rd`` / ``wr`` (bus callbacks for $2000-$7FFF) and ``peek``."""

    def __init__(self, ram: bytearray, prg: bytes, pb: list, rd, wr, peek):
        self.ram, self.prg, self.pb = ram, prg, pb
        self.rd, self.wr, self.peek = rd, wr, peek
        self.a = self.x = self.y = 0
        self.s = 0xFD
        self.pc = 0
        self.cyc = 0
        self.c = self.v = self.d = 0
        self.i = 1
        self.zr = 1                  # Z clear
        self.nr = 0
        self.nmi_pending = False
        self.nmi_delay = False       # NMI enabled by a $2000 write: one more instruction first
        self.irq_line = False
        self.signal = False
        self.stall = 0
        self.jammed = False

    # ------------------------------------------------------------ memory helpers
    def read(self, addr: int) -> int:
        if addr < 0x2000:
            return self.ram[addr & 0x7FF]
        if addr >= 0x8000:
            return self.prg[self.pb[(addr >> 13) & 3] + (addr & 0x1FFF)]
        return self.rd(addr)

    def read16(self, addr: int) -> int:
        return self.read(addr) | (self.read((addr + 1) & 0xFFFF) << 8)

    @property
    def p(self) -> int:
        return ((self.nr & 0x80) | (self.v << 6) | 0x20 | (self.d << 3) | (self.i << 2)
                | (0 if self.zr else 2) | self.c)

    @p.setter
    def p(self, p: int) -> None:
        self.nr = p & 0x80
        self.v, self.d, self.i = (p >> 6) & 1, (p >> 3) & 1, (p >> 2) & 1
        self.zr = 0 if p & 2 else 1
        self.c = p & 1

    def reset(self) -> None:
        self.s = (self.s - 3) & 0xFF if self.pc else 0xFD
        self.i = 1
        self.pc = self.read16(0xFFFC)
        self.cyc += 7
        self.jammed = False

    def _interrupt(self, vector: int) -> None:
        ram, s = self.ram, self.s
        pc = self.pc
        ram[0x100 | s] = pc >> 8
        s = (s - 1) & 0xFF
        ram[0x100 | s] = pc & 0xFF
        s = (s - 1) & 0xFF
        ram[0x100 | s] = self.p & ~0x10
        self.s = (s - 1) & 0xFF
        self.i = 1
        self.pc = self.read16(vector)
        self.cyc += 7

    # ------------------------------------------------------------ running
    def run(self, limit: int) -> None:
        """Run until the cycle counter reaches `limit` (finishing the instruction)."""
        while self.cyc < limit:
            if self.nmi_pending:
                if self.nmi_delay:
                    self.nmi_delay = False
                    run_loop(self, self.cyc + 1)
                    continue
                self.nmi_pending = False
                self._interrupt(0xFFFA)
            elif self.irq_line and not self.i:
                self._interrupt(0xFFFE)
            if self.jammed:
                self.cyc = limit
                return
            run_loop(self, limit)

    def step(self) -> int:
        """Execute exactly one instruction (or interrupt entry); returns cycles."""
        c0 = self.cyc
        if self.nmi_pending:
            self.nmi_pending = False
            self._interrupt(0xFFFA)
        elif self.irq_line and not self.i:
            self._interrupt(0xFFFE)
        else:
            run_loop(self, self.cyc + 1)
        return self.cyc - c0
