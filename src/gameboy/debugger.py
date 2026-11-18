from collections import deque
from src.gameboy.disasm import disassemble, disassemble_range


class Debugger:
    def __init__(self, gb, history_size: int = 32):
        self.gb = gb
        self.cpu = gb.cpu
        self.mmu = gb.mmu
        self.history = deque(maxlen=history_size)   
        self.breakpoints: set[int] = set()


    def step(self) -> None:
        cpu = self.cpu
        self.history.append((cpu.pc, cpu.doctor_line()))
        self.gb.step()

    def run(self, max_steps: int = 50_000_000) -> str:
        for _ in range(max_steps):
            try:
                self.step()
            except Exception as exc:                         
                return f"CRASH: {exc}"
            if self.cpu.pc in self.breakpoints:
                return f"breakpoint at 0x{self.cpu.pc:04X}"
            text = self.mmu.serial_text()
            if "Passed" in text or "Failed" in text:
                return "serial says: " + text.strip().splitlines()[-1]
        return "max steps reached"

    def regs(self) -> str:
        c = self.cpu
        flags = "".join(name if c.f & bit else "-" for name, bit in
                        (("Z", 0x80), ("N", 0x40), ("H", 0x20), ("C", 0x10)))
        return (f"AF={c.af:04X}  BC={c.bc:04X}  DE={c.de:04X}  HL={c.hl:04X}  "
                f"SP={c.sp:04X}  PC={c.pc:04X}  flags={flags}  IME={int(c.ime)}")

    def mem(self, addr: int, length: int = 16) -> str:
        rows = []
        for row in range(addr, addr + length, 16):
            data = [self.mmu.read((row + i) & 0xFFFF) for i in range(16)]
            hexes = " ".join(f"{b:02X}" for b in data)
            text = "".join(chr(b) if 32 <= b < 127 else "." for b in data)
            rows.append(f"0x{row & 0xFFFF:04X}: {hexes}  {text}")
        return "\n".join(rows)

    def show_history(self) -> str:
        lines = []
        for pc, _ in self.history:
            text, _ = disassemble(self.mmu, pc)
            lines.append(f"  0x{pc:04X}: {text}")
        return "\n".join(lines)

    def disasm(self, addr: int | None = None, count: int = 8) -> str:
        return "\n".join(disassemble_range(self.mmu, self.cpu.pc if addr is None else addr, count))

    def console(self, read=input) -> None:
        print(self.HELP)
        print(self.regs())
        print(self.disasm(count=1))
        while True:
            try:
                parts = read("(gbdb) ").strip().split()
            except EOFError:
                break
            if not parts:
                continue
            cmd, args = parts[0], parts[1:]
            try:
                if cmd == "q":
                    break
                elif cmd == "s":
                    for _ in range(int(args[0]) if args else 1):
                        self.step()
                    print(self.regs())
                    print(self.disasm(count=1))
                elif cmd == "c":
                    print(self.run())
                    print(self.regs())
                elif cmd == "b":
                    self.breakpoints.add(int(args[0], 16))
                    print("breakpoints:", ", ".join(f"0x{b:04X}" for b in sorted(self.breakpoints)))
                elif cmd == "d":
                    print(self.disasm(int(args[0], 16) if args else None))
                elif cmd == "m":
                    print(self.mem(int(args[0], 16), int(args[1]) if len(args) > 1 else 16))
                elif cmd == "r":
                    print(self.regs())
                elif cmd == "h":
                    print(self.show_history())
                elif cmd == "serial":
                    print(self.mmu.serial_text() or "(empty)")
                else:
                    print(self.HELP)
            except (ValueError, IndexError):
                print("bad arguments -", self.HELP.splitlines()[0])