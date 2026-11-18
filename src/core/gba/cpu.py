"""ARM7TDMI (ARMv4T): ARM + THUMB.

Each distinct opcode is translated once into a specialised Python function
(source generated from templates, compiled, cached by source text) and then
cached per opcode, so the hot loop is fetch -> cache lookup -> call.

Pipeline: while an instruction executes r[15] holds its address + 8 (ARM) or
+ 4 (THUMB); `cpu.pc` holds the address of the next instruction and branches
write it. Handlers return the cycles they took beyond the opcode fetch.

Memory access goes through the bus region tables R8/R16/R32/W8/W16/W32,
indexed by address >> 24. Reads return aligned values; rotation of unaligned
LDR/LDRH is done here.

Prefetch / self-modifying code: the two instruction slots after the executing
one have already been fetched (r15 reads as +8 in ARM, +4 in THUMB), so a store
to one of those slots updates memory but the slot still executes its
previously-fetched opcode once; it is refetched (and sees the new value) only
afterwards. Any non-sequential fetch (branch, exception) flushes the buffer,
matching the hardware pipeline flush.
"""
from src.core.gba.common import (
    M32, MODE_USR, MODE_FIQ, MODE_IRQ, MODE_SVC, MODE_UND, MODE_SYS, MODE_BANK,
    VEC_UNDEF, VEC_SWI, VEC_IRQ,
)


def _cond_table():
    rows = []
    for c in range(16):
        row = []
        for f in range(16):
            n, z, cf, v = f >> 3 & 1, f >> 2 & 1, f >> 1 & 1, f & 1
            row.append(bool((
                z, not z, cf, not cf, n, not n, v, not v,
                cf and not z, (not cf) or z, n == v, n != v,
                (not z) and n == v, z or n != v, True, False)[c]))
        rows.append(tuple(row))
    return tuple(rows)


COND = _cond_table()

_LOGICAL = {0, 1, 8, 9, 12, 13, 14, 15}
_NZ = "((res >> 28) & 8) | ((res == 0) << 2)"


def _sext(value: int, bits: int) -> int:
    m = 1 << (bits - 1)
    return (value ^ m) - m


class CPU:
    def __init__(self, bus):
        self.bus = bus
        self.r = [0] * 16
        self.f = 0                      # NZCV nibble
        self.irq_disable = 1
        self.fiq_disable = 1
        self.thumb = False
        self.mode = MODE_SVC
        self.bank = MODE_BANK[MODE_SVC]
        self.banked = [[0, 0] for _ in range(6)]      # r13, r14 per bank
        self.hi_usr = [0] * 5                        # r8-r12 outside FIQ
        self.hi_fiq = [0] * 5
        self.spsr = [0] * 6
        self.pc = 0
        self.now = 0                    # cycle counter
        self.limit = 0                  # inner loop exits when now >= limit (0 = re-dispatch)
        self.deadline = 0               # run() target; components may pull it earlier
        self.halted = False
        self.intrwait = False           # HLE IntrWait re-entry (flags already discarded)
        self.bios_latch = 0
        self.hle_swi = None             # callable(cpu, n) -> cycles, or None = real BIOS
        self.arm_cache = {}
        self.thumb_cache = [None] * 65536
        self._make_cache = {}
        self.stale = {}            # prefetched slots: addr -> opcode fetched before a store
        self._pf_next = 0          # next fetch addr if execution stays sequential
        self._ns = {
            "cpu": self, "r": self.r, "M": M32,
            "R8": bus.R8, "R16": bus.R16, "R32": bus.R32,
            "W8": self._wrap_writes(bus.W8, 1),
            "W16": self._wrap_writes(bus.W16, 2),
            "W32": self._wrap_writes(bus.W32, 4),
            "WT32": bus.wait32, "WT16": bus.wait16,
        }

    # ------------------------------------------------------------ prefetch / SMC
    def _wrap_writes(self, table: list, size: int):
        """Per-region write handlers that record pipeline-stale instruction slots
        before delegating to the bus. Fast path: one subtract + compare."""
        cpu = self
        mark = self._smc_store

        def make(fn):
            def w(a, v, _fn=fn):
                if -4 < a - cpu.pc < 8:            # may overlap a prefetched slot
                    mark(a, size)
                _fn(a, v)
            return w
        return [make(fn) for fn in table]

    def _smc_store(self, a: int, size: int) -> None:
        """A CPU write overlaps the instruction slots already in the pipeline:
        those slots execute the opcode they were fetched with, once."""
        step = 2 if self.thumb else 4
        end = a + size
        stale = self.stale
        bus = self.bus
        pc = self.pc
        for s in (pc, pc + step):                  # slots ahead of the executing one
            if a < s + step and s < end and s not in stale:
                reg = (s >> 24) & 0xFF
                stale[s] = (bus.R16[reg](s) if step == 2 else bus.R32[reg](s))
        if stale:
            self._pf_next = pc                     # next fetch if no branch intervenes

    # ------------------------------------------------------------ state
    def get_cpsr(self) -> int:
        return ((self.f << 28) | (self.irq_disable << 7) | (self.fiq_disable << 6)
                | (self.thumb << 5) | self.mode)

    def set_cpsr(self, v: int) -> None:
        self.f = (v >> 28) & 15
        self.irq_disable = (v >> 7) & 1
        self.fiq_disable = (v >> 6) & 1
        self.thumb = bool(v & 0x20)
        self.switch_mode((v & 0x1F) | 0x10)
        self.limit = 0                  # thumb / IRQ state may have changed

    def get_spsr(self) -> int:
        return self.spsr[self.bank] if self.bank else self.get_cpsr()

    def set_spsr(self, v: int) -> None:
        if self.bank:
            self.spsr[self.bank] = v & M32

    def switch_mode(self, mode: int) -> None:
        nb = MODE_BANK.get(mode, 0)
        ob = self.bank
        if nb != ob:
            r = self.r
            b = self.banked[ob]
            b[0], b[1] = r[13], r[14]
            b = self.banked[nb]
            r[13], r[14] = b[0], b[1]
            if ob == 1:
                self.hi_fiq[:] = r[8:13]
                r[8:13] = self.hi_usr
            elif nb == 1:
                self.hi_usr[:] = r[8:13]
                r[8:13] = self.hi_fiq
            self.bank = nb
        self.mode = mode

    def ret_spsr(self, target: int) -> None:
        """Data-processing / LDM with S and PC: CPSR = SPSR, then branch."""
        if self.bank:
            self.set_cpsr(self.spsr[self.bank])
        self.pc = target & (0xFFFFFFFE if self.thumb else 0xFFFFFFFC)
        self.stale.clear()                         # pipeline flush
        self.limit = 0

    def user_reg(self, i: int) -> int:
        if i == 15 or i < 8 or self.bank == 0:
            return self.r[i]
        if i >= 13:
            return self.banked[0][i - 13]
        return self.hi_usr[i - 8] if self.bank == 1 else self.r[i]

    def set_user_reg(self, i: int, v: int) -> None:
        if i < 8 or self.bank == 0:
            self.r[i] = v
        elif i >= 13:
            self.banked[0][i - 13] = v
        elif self.bank == 1:
            self.hi_usr[i - 8] = v
        else:
            self.r[i] = v

    def exception(self, vector: int, mode: int, lr: int) -> None:
        cpsr = self.get_cpsr()
        self.switch_mode(mode)
        self.spsr[self.bank] = cpsr
        self.r[14] = lr & M32
        self.thumb = False
        self.irq_disable = 1
        if mode == MODE_FIQ:
            self.fiq_disable = 1
        self.pc = vector
        self.stale.clear()                         # pipeline flush
        self.limit = 0

    def swi(self, n: int) -> int:
        if self.hle_swi is not None:
            return self.hle_swi(self, n)
        self.exception(VEC_SWI, MODE_SVC, self.pc)
        return 3

    def undefined(self) -> int:
        self.exception(VEC_UNDEF, MODE_UND, self.pc)
        return 3

    def reset_skip_bios(self, entry: int, sp_usr: int, sp_irq: int, sp_svc: int) -> None:
        self.switch_mode(MODE_IRQ)
        self.r[13] = sp_irq
        self.switch_mode(MODE_SVC)
        self.r[13] = sp_svc
        self.switch_mode(MODE_SYS)
        self.r[13] = sp_usr
        self.irq_disable = 0
        self.fiq_disable = 0
        self.thumb = False
        self.pc = entry

    # ------------------------------------------------------------ run loop
    def run(self, limit: int) -> None:
        """Run until `now` reaches limit, or an earlier `deadline` set meanwhile
        (e.g. a timer started by the program)."""
        bus = self.bus
        self.deadline = limit
        while self.now < self.deadline:
            if self.halted:
                if bus.ie & bus.if_:
                    self.halted = False
                else:
                    self.now = self.deadline
                    return
            if bus.ime and bus.ie & bus.if_ and not self.irq_disable:
                self.exception(VEC_IRQ, MODE_IRQ, self.pc + 4)
            self.limit = self.deadline
            if self.thumb:
                self._run_thumb()
            else:
                self._run_arm()

    def _run_arm(self) -> None:
        r = self.r
        cache = self.arm_cache
        cond = COND
        decode = self._decode_arm
        views = self.bus.code32
        stale = self.stale
        creg = -1
        mem = None
        mask = 0
        ft = 1
        pc = self.pc
        while self.now < self.limit:
            reg = pc >> 24
            if reg != creg:
                creg = reg
                mem, mask, ft = views(reg)
            if mem is None:
                op = self._slow_fetch32(pc)
            else:
                op = mem[(pc & mask) >> 2]
            if stale:
                if pc != self._pf_next:            # non-sequential: pipeline flush
                    stale.clear()
                else:
                    op = stale.pop(pc, op)         # serve the already-fetched opcode
                self._pf_next = pc + 4
            r[15] = pc + 8
            self.pc = pc + 4
            c = op >> 28
            if c == 14 or cond[c][self.f]:
                fn = cache.get(op)
                if fn is None:
                    fn = decode(op)
                self.now += fn() + ft
            else:
                self.now += ft
            pc = self.pc

    def _run_thumb(self) -> None:
        r = self.r
        cache = self.thumb_cache
        decode = self._decode_thumb
        views = self.bus.code16
        stale = self.stale
        creg = -1
        mem = None
        mask = 0
        ft = 1
        pc = self.pc
        while self.now < self.limit:
            reg = pc >> 24
            if reg != creg:
                creg = reg
                mem, mask, ft = views(reg)
            if mem is None:
                op = self._slow_fetch16(pc)
            else:
                op = mem[(pc & mask) >> 1]
            if stale:
                if pc != self._pf_next:            # non-sequential: pipeline flush
                    stale.clear()
                else:
                    op = stale.pop(pc, op)         # serve the already-fetched opcode
                self._pf_next = pc + 2
            r[15] = pc + 4
            self.pc = pc + 2
            fn = cache[op]
            if fn is None:
                fn = decode(op)
            self.now += fn() + ft
            pc = self.pc

    def _slow_fetch32(self, pc: int) -> int:
        bus = self.bus
        if pc < 0x4000:
            self.bios_latch = bus.bios32[((pc + 8) & 0x3FFF) >> 2]
            return bus.bios32[pc >> 2]
        return bus.R32[(pc >> 24) & 0xFF](pc)

    def _slow_fetch16(self, pc: int) -> int:
        bus = self.bus
        if pc < 0x4000:
            self.bios_latch = bus.bios32[((pc + 4) & 0x3FFC) >> 2]
            return bus.bios16[pc >> 1]
        return bus.R16[(pc >> 24) & 0xFF](pc)

    def step(self) -> int:
        """Execute exactly one instruction (tests / debugger); returns cycles."""
        t0 = self.now
        self.run(self.now + 1)
        return self.now - t0

    # ------------------------------------------------------------ code generation
    def _build(self, body: list[str], params: dict | None = None):
        params = params or {}
        names = tuple(params)
        src = ("def make(" + ", ".join(names) + "):\n def f():\n"
               + "\n".join("  " + line for line in body) + "\n return f\n")
        make = self._make_cache.get(src)
        if make is None:
            ns = dict(self._ns)
            exec(compile(src, "<gba-cpu>", "exec"), ns)
            make = self._make_cache[src] = ns["make"]
        return make(*params.values())

    def _decode_arm(self, op: int):
        fn = self._gen_arm(op)
        self.arm_cache[op] = fn
        return fn

    def _decode_thumb(self, op: int):
        fn = self._gen_thumb(op)
        self.thumb_cache[op] = fn
        return fn

    # ------------------------------------------------------------ ARM
    def _gen_arm(self, op: int):
        top = (op >> 25) & 7
        if (op & 0x0FFFFFF0) == 0x012FFF10:
            return self._arm_bx(op)
        if top == 0:
            if (op & 0x0FC000F0) == 0x00000090:
                return self._arm_mul(op)
            if (op & 0x0F8000F0) == 0x00800090:
                return self._arm_mull(op)
            if (op & 0x0FB00FF0) == 0x01000090:
                return self._arm_swp(op)
            if (op & 0x90) == 0x90 and (op & 0x60):
                return self._arm_half(op)
            if (op & 0x01900000) == 0x01000000:
                return self._arm_psr(op)
            return self._arm_dp(op)
        if top == 1:
            if (op & 0x01900000) == 0x01000000:
                if op & 0x00200000:
                    return self._arm_psr(op)
                return self._build(["return 1"])
            return self._arm_dp(op)
        if top == 2 or (top == 3 and not op & 0x10):
            return self._arm_sdt(op)
        if top == 4:
            return self._arm_block(op)
        if top == 5:
            return self._arm_branch(op)
        if top == 7 and op & 0x01000000:
            return self._build(["return cpu.swi(N)"], {"N": (op >> 16) & 0xFF})
        return self._build(["return cpu.undefined()"])

    @staticmethod
    def _shift_imm_code(rm: int, typ: int, amt: int, need_c: bool, out: str = "b"):
        """Register shifted by immediate. Returns (lines, carry_mode) with carry_mode
        'var' (sc set), or 'keep' (shifter carry = current C)."""
        v = f"r[{rm}]"
        L = []
        if typ == 0:
            if amt == 0:
                return [f"{out} = {v}"], "keep"
            L.append(f"v = {v}")
            L.append(f"{out} = (v << {amt}) & M")
            if need_c:
                L.append(f"sc = (v >> {32 - amt}) & 1")
        elif typ == 1:
            L.append(f"v = {v}")
            if amt == 0:
                L.append(f"{out} = 0")
                if need_c:
                    L.append("sc = v >> 31")
            else:
                L.append(f"{out} = v >> {amt}")
                if need_c:
                    L.append(f"sc = (v >> {amt - 1}) & 1")
        elif typ == 2:
            L.append(f"v = {v}")
            if amt == 0:
                L.append(f"{out} = M if v >> 31 else 0")
                if need_c:
                    L.append("sc = v >> 31")
            else:
                L.append(f"{out} = (((v ^ 0x80000000) - 0x80000000) >> {amt}) & M")
                if need_c:
                    L.append(f"sc = (v >> {amt - 1}) & 1")
        else:
            L.append(f"v = {v}")
            if amt == 0:
                L.append(f"{out} = (v >> 1) | ((cpu.f & 2) << 30)")
                if need_c:
                    L.append("sc = v & 1")
            else:
                L.append(f"{out} = ((v >> {amt}) | (v << {32 - amt})) & M")
                if need_c:
                    L.append(f"sc = (v >> {amt - 1}) & 1")
        return L, ("var" if need_c else "keep")

    @staticmethod
    def _shift_reg_code(vexpr: str, sexpr: str, typ: int, need_c: bool, out: str = "b"):
        """Value shifted by register amount (low byte). Always sets sc if need_c."""
        L = [f"v = {vexpr}", f"s = {sexpr}"]
        C = "((cpu.f >> 1) & 1)"
        if typ == 0:
            if need_c:
                L += [f"if s == 0: {out} = v; sc = {C}",
                      f"elif s < 32: {out} = (v << s) & M; sc = (v >> (32 - s)) & 1",
                      f"elif s == 32: {out} = 0; sc = v & 1",
                      f"else: {out} = 0; sc = 0"]
            else:
                L += [f"{out} = (v << s) & M if s < 32 else 0"]
        elif typ == 1:
            if need_c:
                L += [f"if s == 0: {out} = v; sc = {C}",
                      f"elif s < 32: {out} = v >> s; sc = (v >> (s - 1)) & 1",
                      f"elif s == 32: {out} = 0; sc = v >> 31",
                      f"else: {out} = 0; sc = 0"]
            else:
                L += [f"{out} = v >> s if s < 32 else 0"]
        elif typ == 2:
            if need_c:
                L += [f"if s == 0: {out} = v; sc = {C}",
                      f"elif s < 32: {out} = (((v ^ 0x80000000) - 0x80000000) >> s) & M; sc = (v >> (s - 1)) & 1",
                      f"else: {out} = M if v >> 31 else 0; sc = v >> 31"]
            else:
                L += [f"{out} = (((v ^ 0x80000000) - 0x80000000) >> (s if s < 32 else 31)) & M"]
        else:
            if need_c:
                L += [f"if s == 0: {out} = v; sc = {C}",
                      "else:",
                      " s &= 31",
                      f" if s == 0: {out} = v; sc = v >> 31",
                      f" else: {out} = ((v >> s) | (v << (32 - s))) & M; sc = (v >> (s - 1)) & 1"]
            else:
                L += ["s &= 31",
                      f"{out} = ((v >> s) | (v << (32 - s))) & M if s else v"]
        return L

    @staticmethod
    def _alu_code(opc: int, s: bool, carry_mode: str):
        """Lines computing `res` from a, b (and flags if s). Returns (lines, writes_rd)."""
        L = []
        writes = opc not in (8, 9, 10, 11)
        C = "((cpu.f >> 1) & 1)"
        if opc in _LOGICAL:
            expr = {0: "a & b", 1: "a ^ b", 8: "a & b", 9: "a ^ b", 12: "a | b",
                    13: "b", 14: "a & (b ^ M)", 15: "b ^ M"}[opc]
            L.append(f"res = {expr}")
            if s:
                if carry_mode == "var":
                    L.append(f"cpu.f = {_NZ} | (sc << 1) | (cpu.f & 1)")
                else:
                    L.append(f"cpu.f = {_NZ} | (cpu.f & 3)")
            return L, writes
        if opc in (4, 11):          # ADD / CMN
            L += ["t = a + b", "res = t & M"]
            if s:
                L.append(f"cpu.f = {_NZ} | ((t >> 32) << 1) | (((a ^ res) & (b ^ res)) >> 31)")
        elif opc == 5:              # ADC
            L += [f"t = a + b + {C}", "res = t & M"]
            if s:
                L.append(f"cpu.f = {_NZ} | ((t >> 32) << 1) | (((a ^ res) & (b ^ res)) >> 31)")
        elif opc in (2, 10):        # SUB / CMP
            L += ["res = (a - b) & M"]
            if s:
                L.append(f"cpu.f = {_NZ} | ((a >= b) << 1) | (((a ^ b) & (a ^ res)) >> 31)")
        elif opc == 3:              # RSB
            L += ["res = (b - a) & M"]
            if s:
                L.append(f"cpu.f = {_NZ} | ((b >= a) << 1) | (((a ^ b) & (b ^ res)) >> 31)")
        elif opc == 6:              # SBC
            L += [f"t = a - b - 1 + {C}", "res = t & M"]
            if s:
                L.append(f"cpu.f = {_NZ} | ((t >= 0) << 1) | (((a ^ b) & (a ^ res)) >> 31)")
        elif opc == 7:              # RSC
            L += [f"t = b - a - 1 + {C}", "res = t & M"]
            if s:
                L.append(f"cpu.f = {_NZ} | ((t >= 0) << 1) | (((a ^ b) & (b ^ res)) >> 31)")
        return L, writes

    def _arm_dp(self, op: int):
        opc = (op >> 21) & 15
        s = bool(op & 0x00100000)
        rn = (op >> 16) & 15
        rd = (op >> 12) & 15
        need_c = s and opc in _LOGICAL and rd != 15
        params = {}
        L = []
        cycles = 1
        reg_shift = False
        if op & 0x02000000:
            rot = ((op >> 8) & 15) * 2
            imm = op & 0xFF
            if rot:
                imm = ((imm >> rot) | (imm << (32 - rot))) & M32
            params["IMM"] = imm
            L.append("b = IMM")
            carry = "keep"
            if need_c and rot:
                L.append(f"sc = {imm >> 31}")
                carry = "var"
        else:
            rm = op & 15
            typ = (op >> 5) & 3
            if op & 0x10:
                reg_shift = True
                cycles += 1
                vexpr = f"r[{rm}]" + (" + 4" if rm == 15 else "")
                L += self._shift_reg_code(vexpr, f"r[{(op >> 8) & 15}] & 255", typ, need_c)
                carry = "var" if need_c else "keep"
            else:
                lines, carry = self._shift_imm_code(rm, typ, (op >> 7) & 31, need_c)
                L += lines
        if opc not in (13, 15):
            L.append(f"a = r[{rn}]" + (" + 4" if rn == 15 and reg_shift else ""))
        alu, writes = self._alu_code(opc, s and rd != 15, carry)
        L += alu
        if writes:
            if rd == 15:
                if s:
                    L.append("cpu.ret_spsr(res)")
                else:
                    L.append("cpu.pc = res & 0xFFFFFFFC")
                cycles += 2
            else:
                L.append(f"r[{rd}] = res")
        elif rd == 15 and s:                     # TSTP/CMPP...: CPSR = SPSR, no pipeline flush
            L.append("if cpu.bank: cpu.set_cpsr(cpu.spsr[cpu.bank])")
        L.append(f"return {cycles}")
        return self._build(L, params)

    def _arm_psr(self, op: int):
        use_spsr = bool(op & 0x00400000)
        if not op & 0x00200000:                   # MRS
            rd = (op >> 12) & 15
            src = "cpu.get_spsr()" if use_spsr else "cpu.get_cpsr()"
            return self._build([f"r[{rd}] = {src}", "return 1"])
        mask = 0
        if op & 0x00080000:
            mask |= 0xFF000000
        if op & 0x00040000:
            mask |= 0x00FF0000
        if op & 0x00020000:
            mask |= 0x0000FF00
        if op & 0x00010000:
            mask |= 0x000000FF
        params = {"MASK": mask}
        if op & 0x02000000:
            rot = ((op >> 8) & 15) * 2
            imm = op & 0xFF
            if rot:
                imm = ((imm >> rot) | (imm << (32 - rot))) & M32
            params["IMM"] = imm
            L = ["v = IMM"]
        else:
            L = [f"v = r[{op & 15}]"]
        if use_spsr:
            L += ["if cpu.bank: cpu.spsr[cpu.bank] = (cpu.spsr[cpu.bank] & ~MASK) | (v & MASK)"]
        else:
            L += ["m = MASK & 0xFF000000 if cpu.mode == 0x10 else MASK",
                  "cpu.set_cpsr((cpu.get_cpsr() & ~m) | (v & m))"]
        L.append("return 1")
        return self._build(L, params)

    def _arm_mul(self, op: int):
        rd = (op >> 16) & 15
        rn = (op >> 12) & 15
        rs = (op >> 8) & 15
        rm = op & 15
        acc = bool(op & 0x00200000)
        L = [f"res = (r[{rm}] * r[{rs}]" + (f" + r[{rn}]" if acc else "") + ") & M",
             f"r[{rd}] = res"]
        if op & 0x00100000:
            L.append(f"cpu.f = {_NZ} | (cpu.f & 3)")
        L.append(f"return {3 if acc else 2}")
        return self._build(L)

    def _arm_mull(self, op: int):
        hi = (op >> 16) & 15
        lo = (op >> 12) & 15
        rs = (op >> 8) & 15
        rm = op & 15
        signed = bool(op & 0x00400000)
        acc = bool(op & 0x00200000)
        if signed:
            L = [f"a = r[{rm}]; a = (a ^ 0x80000000) - 0x80000000",
                 f"b = r[{rs}]; b = (b ^ 0x80000000) - 0x80000000"]
        else:
            L = [f"a = r[{rm}]", f"b = r[{rs}]"]
        L.append("t = a * b" + (f" + ((r[{hi}] << 32) | r[{lo}])" if acc else ""))
        L += ["t &= 0xFFFFFFFFFFFFFFFF", f"r[{lo}] = t & M", f"r[{hi}] = t >> 32"]
        if op & 0x00100000:
            L.append("cpu.f = ((t >> 60) & 8) | ((t == 0) << 2) | (cpu.f & 3)")
        L.append(f"return {4 if acc else 3}")
        return self._build(L)

    def _arm_swp(self, op: int):
        rn = (op >> 16) & 15
        rd = (op >> 12) & 15
        rm = op & 15
        if op & 0x00400000:
            L = [f"a = r[{rn}]", "v = R8[a >> 24](a)",
                 f"W8[a >> 24](a, r[{rm}] & 255)", f"r[{rd}] = v"]
        else:
            L = [f"a = r[{rn}]", "v = R32[a >> 24](a)", "rot = (a & 3) << 3",
                 "if rot: v = ((v >> rot) | (v << (32 - rot))) & M",
                 f"W32[a >> 24](a, r[{rm}])", f"r[{rd}] = v"]
        L.append("return 4 + 2 * WT32[a >> 24]")
        return self._build(L)

    def _arm_addr(self, op: int, rn: int, off_lines: list, off: str, L: list):
        """Addressing for single/halfword transfers. Sets `a` (access address) and
        returns writeback expression or None."""
        up = bool(op & 0x00800000)
        pre = bool(op & 0x01000000)
        wb = (not pre) or bool(op & 0x00200000)
        L += off_lines
        L.append(f"base = r[{rn}]")
        sign = "+" if up else "-"
        if pre:
            L.append(f"a = (base {sign} {off}) & M")
            return "a" if wb else None
        L.append("a = base")
        return f"(base {sign} {off}) & M"

    def _arm_sdt(self, op: int):
        rn = (op >> 16) & 15
        rd = (op >> 12) & 15
        load = bool(op & 0x00100000)
        byte = bool(op & 0x00400000)
        params = {}
        if op & 0x02000000:
            lines, _ = self._shift_imm_code(op & 15, (op >> 5) & 3, (op >> 7) & 31, False, out="o")
            off = "o"
        else:
            lines = []
            params["OFF"] = op & 0xFFF
            off = "OFF"
        L = []
        wb = self._arm_addr(op, rn, lines, off, L)
        if load:
            if byte:
                L.append("v = R8[a >> 24](a)")
            else:
                L += ["v = R32[a >> 24](a)", "rot = (a & 3) << 3",
                      "if rot: v = ((v >> rot) | (v << (32 - rot))) & M"]
            if wb is not None and rn != rd:
                L.append(f"r[{rn}] = {wb}")
            if rd == 15:
                L += ["cpu.pc = v & 0xFFFFFFFC", "return 5 + WT32[a >> 24]"]
            else:
                L += [f"r[{rd}] = v", "return 2 + WT32[a >> 24]"]
        else:
            val = f"r[{rd}]" + (" + 4" if rd == 15 else "")
            if byte:
                L.append(f"W8[a >> 24](a, ({val}) & 255)")
            else:
                L.append(f"W32[a >> 24](a, {val})")
            if wb is not None:
                L.append(f"r[{rn}] = {wb}")
            L.append("return 1 + WT32[a >> 24]")
        return self._build(L, params)

    def _arm_half(self, op: int):
        rn = (op >> 16) & 15
        rd = (op >> 12) & 15
        load = bool(op & 0x00100000)
        sh = (op >> 5) & 3
        params = {}
        if op & 0x00400000:
            params["OFF"] = ((op >> 4) & 0xF0) | (op & 15)
            off, lines = "OFF", []
        else:
            off, lines = "o", [f"o = r[{op & 15}]"]   # read before a load can clobber it
        L = []
        wb = self._arm_addr(op, rn, lines, off, L)
        if load:
            if sh == 1:
                L += ["v = R16[a >> 24](a)",
                      "if a & 1: v = ((v >> 8) | (v << 24)) & M"]
            elif sh == 2:
                L += ["v = R8[a >> 24](a)", "v = ((v ^ 0x80) - 0x80) & M"]
            else:
                L += ["if a & 1: v = R8[a >> 24](a); v = ((v ^ 0x80) - 0x80) & M",
                      "else: v = R16[a >> 24](a); v = ((v ^ 0x8000) - 0x8000) & M"]
            if wb is not None and rn != rd:
                L.append(f"r[{rn}] = {wb}")
            if rd == 15:
                L += ["cpu.pc = v & 0xFFFFFFFC", "return 5 + WT16[a >> 24]"]
            else:
                L += [f"r[{rd}] = v", "return 2 + WT16[a >> 24]"]
        else:
            val = f"r[{rd}]" + (" + 4" if rd == 15 else "")
            if sh == 1:
                L.append(f"W16[a >> 24](a, ({val}) & 0xFFFF)")
            # sh 2/3 store forms are LDRD/STRD on ARMv5: no-op here
            if wb is not None:
                L.append(f"r[{rn}] = {wb}")
            L.append("return 1 + WT16[a >> 24]")
        return self._build(L, params)

    def _arm_block(self, op: int):
        rn = (op >> 16) & 15
        rlist = op & 0xFFFF
        load = bool(op & 0x00100000)
        wb = bool(op & 0x00200000)
        user = bool(op & 0x00400000)
        up = bool(op & 0x00800000)
        pre = bool(op & 0x01000000)
        regs = [i for i in range(16) if rlist >> i & 1]
        size = 4 * len(regs)
        if not regs:                       # ARM7 quirk: transfers r15, base moves 0x40
            regs = [15]
            size = 0x40
        if up:
            start = "base + 4" if pre else "base"
            new = f"(base + {size}) & M"
        else:
            start = f"base - {size}" + (" " if pre else " + 4")
            new = f"(base - {size}) & M"
        L = [f"base = r[{rn}]", f"a = ({start}) & M", "w = WT32[a >> 24]"]
        user_bank = user and not (load and 15 in regs)
        if load:
            if wb and rn not in regs:
                L.append(f"r[{rn}] = {new}")
            for i in regs:
                L.append("v = R32[a >> 24](a)")
                if i == 15:
                    if user:
                        L.append("cpu.ret_spsr(v)")
                    else:
                        L.append("cpu.pc = v & 0xFFFFFFFC")
                elif user_bank and i >= 8:
                    L.append(f"cpu.set_user_reg({i}, v)")
                else:
                    L.append(f"r[{i}] = v")
                L.append("a = (a + 4) & M")
        else:
            first = regs[0]
            for i in regs:
                if i == 15:
                    val = "r[15] + 4"
                elif i == rn and wb and i != first:
                    val = new
                elif user_bank and i >= 8:
                    val = f"cpu.user_reg({i})"
                else:
                    val = f"r[{i}]"
                L.append(f"W32[a >> 24](a, {val})")
                L.append("a = (a + 4) & M")
            if wb:
                L.append(f"r[{rn}] = {new}")
        L.append(f"return {len(regs) + (3 if load else 1)} + {len(regs)} * w")
        return self._build(L)

    def _arm_branch(self, op: int):
        off = _sext(op & 0xFFFFFF, 24) << 2
        L = []
        if op & 0x01000000:
            L.append("r[14] = r[15] - 4")
        L += ["cpu.pc = (r[15] + OFF) & M", "return 2"]
        return self._build(L, {"OFF": off})

    def _arm_bx(self, op: int):
        rm = op & 15
        return self._build([
            f"v = r[{rm}]",
            "if v & 1: cpu.thumb = True; cpu.pc = v & 0xFFFFFFFE; cpu.limit = 0",
            "else: cpu.pc = v & 0xFFFFFFFC",
            "return 2"])

    # ------------------------------------------------------------ THUMB
    def _gen_thumb(self, op: int):
        top = op >> 13
        if top == 0:
            if (op >> 11) == 3:
                return self._t_addsub(op)
            return self._t_shift(op)
        if top == 1:
            return self._t_imm(op)
        if top == 2:
            if (op >> 10) == 0x10:
                return self._t_alu(op)
            if (op >> 10) == 0x11:
                return self._t_hireg(op)
            if (op >> 11) == 9:
                rd = (op >> 8) & 7
                return self._build([
                    "a = (r[15] & 0xFFFFFFFC) + IMM", "v = R32[a >> 24](a)",
                    f"r[{rd}] = v", "return 2 + WT32[a >> 24]"], {"IMM": (op & 0xFF) << 2})
            return self._t_ldst_reg(op)
        if top == 3:
            return self._t_ldst_imm(op)
        if top == 4:
            if op & 0x1000:
                return self._t_sp_rel(op)
            return self._t_ldst_half(op)
        if top == 5:
            if not op & 0x1000:
                rd = (op >> 8) & 7
                src = "r[13]" if op & 0x0800 else "(r[15] & 0xFFFFFFFC)"
                return self._build([f"r[{rd}] = ({src} + IMM) & M", "return 0"],
                                   {"IMM": (op & 0xFF) << 2})
            if (op >> 8) == 0xB0:
                off = (op & 0x7F) << 2
                if op & 0x80:
                    off = -off
                return self._build(["r[13] = (r[13] + IMM) & M", "return 0"], {"IMM": off})
            if (op & 0x0600) == 0x0400:
                return self._t_pushpop(op)
            return self._build(["return cpu.undefined()"])
        if top == 6:
            if not op & 0x1000:
                return self._t_ldm_stm(op)
            cond = (op >> 8) & 15
            if cond == 15:
                return self._build(["return cpu.swi(N)"], {"N": op & 0xFF})
            if cond == 14:
                return self._build(["return cpu.undefined()"])
            off = _sext(op & 0xFF, 8) << 1
            return self._build([
                "if CT[cpu.f]: cpu.pc = (r[15] + OFF) & M; return 2",
                "return 0"], {"CT": COND[cond], "OFF": off})
        # top == 7
        h = (op >> 11) & 3
        if h == 0:
            return self._build(["cpu.pc = (r[15] + OFF) & M", "return 2"],
                               {"OFF": _sext(op & 0x7FF, 11) << 1})
        if h == 2:
            return self._build(["r[14] = (r[15] + OFF) & M", "return 0"],
                               {"OFF": _sext(op & 0x7FF, 11) << 12})
        if h == 3:
            return self._build([
                "nxt = r[15] - 2", "cpu.pc = (r[14] + OFF) & M", "r[14] = nxt | 1", "return 2"],
                {"OFF": (op & 0x7FF) << 1})
        return self._build(["return cpu.undefined()"])

    def _t_shift(self, op: int):
        typ = (op >> 11) & 3
        amt = (op >> 6) & 31
        rs = (op >> 3) & 7
        rd = op & 7
        lines, carry = self._shift_imm_code(rs, typ, amt, True, out="res")
        L = list(lines)
        if carry == "var":
            L.append(f"cpu.f = {_NZ} | (sc << 1) | (cpu.f & 1)")
        else:
            L.append(f"cpu.f = {_NZ} | (cpu.f & 3)")
        L += [f"r[{rd}] = res", "return 0"]
        return self._build(L)

    def _t_addsub(self, op: int):
        imm = bool(op & 0x0400)
        sub = bool(op & 0x0200)
        rn = (op >> 6) & 7
        rs = (op >> 3) & 7
        rd = op & 7
        params = {}
        L = [f"a = r[{rs}]"]
        if imm:
            params["IMM"] = rn
            L.append("b = IMM")
        else:
            L.append(f"b = r[{rn}]")
        alu, _ = self._alu_code(2 if sub else 4, True, "keep")
        L += alu + [f"r[{rd}] = res", "return 0"]
        return self._build(L, params)

    def _t_imm(self, op: int):
        opc = (op >> 11) & 3
        rd = (op >> 8) & 7
        L = [f"a = r[{rd}]", "b = IMM"]
        alu, writes = self._alu_code((13, 10, 4, 2)[opc], True, "keep")
        L += alu
        if writes:
            L.append(f"r[{rd}] = res")
        L.append("return 0")
        return self._build(L, {"IMM": op & 0xFF})

    def _t_alu(self, op: int):
        opc = (op >> 6) & 15
        rs = (op >> 3) & 7
        rd = op & 7
        L = [f"a = r[{rd}]", f"b = r[{rs}]"]
        cyc = 0
        if opc in (2, 3, 4, 7):                  # LSL LSR ASR ROR by register
            typ = {2: 0, 3: 1, 4: 2, 7: 3}[opc]
            L = self._shift_reg_code(f"r[{rd}]", f"r[{rs}] & 255", typ, True, out="res")
            L += [f"cpu.f = {_NZ} | (sc << 1) | (cpu.f & 1)", f"r[{rd}] = res"]
            cyc = 1
        elif opc == 9:                           # NEG = 0 - rs
            L = ["a = 0", f"b = r[{rs}]"]
            alu, _ = self._alu_code(2, True, "keep")
            L += alu + [f"r[{rd}] = res"]
        elif opc == 13:                          # MUL
            L += ["res = (a * b) & M", f"cpu.f = {_NZ} | (cpu.f & 3)", f"r[{rd}] = res"]
            cyc = 2
        else:
            arm = {0: 0, 1: 1, 5: 5, 6: 6, 8: 8, 10: 10, 11: 11, 12: 12, 14: 14, 15: 15}[opc]
            alu, writes = self._alu_code(arm, True, "keep")
            L += alu
            if writes:
                L.append(f"r[{rd}] = res")
        L.append(f"return {cyc}")
        return self._build(L)

    def _t_hireg(self, op: int):
        opc = (op >> 8) & 3
        rd = (op & 7) | ((op >> 4) & 8)
        rs = (op >> 3) & 15
        if opc == 3:                             # BX
            return self._build([
                f"v = r[{rs}]",
                "if v & 1: cpu.pc = v & 0xFFFFFFFE",
                "else: cpu.thumb = False; cpu.pc = v & 0xFFFFFFFC; cpu.limit = 0",
                "return 2"])
        if opc == 1:                             # CMP
            L = [f"a = r[{rd}]", f"b = r[{rs}]"]
            alu, _ = self._alu_code(10, True, "keep")
            return self._build(L + alu + ["return 0"])
        if opc == 0:
            L = [f"res = (r[{rd}] + r[{rs}]) & M"]
        else:
            L = [f"res = r[{rs}]"]
        if rd == 15:
            L += ["cpu.pc = res & 0xFFFFFFFE", "return 2"]
        else:
            L += [f"r[{rd}] = res", "return 0"]
        return self._build(L)

    def _t_ldst_reg(self, op: int):
        ro = (op >> 6) & 7
        rb = (op >> 3) & 7
        rd = op & 7
        L = [f"a = (r[{rb}] + r[{ro}]) & M"]
        if op & 0x0200:                          # sign-extended / halfword group
            kind = (op >> 10) & 3
            if kind == 0:
                L += [f"W16[a >> 24](a, r[{rd}] & 0xFFFF)", "return 1 + WT16[a >> 24]"]
                return self._build(L)
            if kind == 1:
                L += ["v = R8[a >> 24](a)", "v = ((v ^ 0x80) - 0x80) & M"]
            elif kind == 2:
                L += ["v = R16[a >> 24](a)", "if a & 1: v = ((v >> 8) | (v << 24)) & M"]
            else:
                L += ["if a & 1: v = R8[a >> 24](a); v = ((v ^ 0x80) - 0x80) & M",
                      "else: v = R16[a >> 24](a); v = ((v ^ 0x8000) - 0x8000) & M"]
            L += [f"r[{rd}] = v", "return 2 + WT16[a >> 24]"]
            return self._build(L)
        load = bool(op & 0x0800)
        byte = bool(op & 0x0400)
        return self._build(L + self._t_access(rd, load, byte))

    @staticmethod
    def _t_access(rd: int, load: bool, byte: bool) -> list[str]:
        if load:
            if byte:
                return ["v = R8[a >> 24](a)", f"r[{rd}] = v", "return 2 + WT16[a >> 24]"]
            return ["v = R32[a >> 24](a)", "rot = (a & 3) << 3",
                    "if rot: v = ((v >> rot) | (v << (32 - rot))) & M",
                    f"r[{rd}] = v", "return 2 + WT32[a >> 24]"]
        if byte:
            return [f"W8[a >> 24](a, r[{rd}] & 255)", "return 1 + WT16[a >> 24]"]
        return [f"W32[a >> 24](a, r[{rd}])", "return 1 + WT32[a >> 24]"]

    def _t_ldst_imm(self, op: int):
        byte = bool(op & 0x1000)
        load = bool(op & 0x0800)
        off = (op >> 6) & 31
        if not byte:
            off <<= 2
        rb = (op >> 3) & 7
        rd = op & 7
        L = [f"a = (r[{rb}] + IMM) & M"]
        return self._build(L + self._t_access(rd, load, byte), {"IMM": off})

    def _t_ldst_half(self, op: int):
        load = bool(op & 0x0800)
        rb = (op >> 3) & 7
        rd = op & 7
        L = [f"a = (r[{rb}] + IMM) & M"]
        if load:
            L += ["v = R16[a >> 24](a)", "if a & 1: v = ((v >> 8) | (v << 24)) & M",
                  f"r[{rd}] = v", "return 2 + WT16[a >> 24]"]
        else:
            L += [f"W16[a >> 24](a, r[{rd}] & 0xFFFF)", "return 1 + WT16[a >> 24]"]
        return self._build(L, {"IMM": ((op >> 6) & 31) << 1})

    def _t_sp_rel(self, op: int):
        load = bool(op & 0x0800)
        rd = (op >> 8) & 7
        L = ["a = (r[13] + IMM) & M"]
        return self._build(L + self._t_access(rd, load, False), {"IMM": (op & 0xFF) << 2})

    def _t_pushpop(self, op: int):
        load = bool(op & 0x0800)
        extra = bool(op & 0x0100)
        regs = [i for i in range(8) if op >> i & 1]
        if load:
            if extra:
                regs.append(15)
            size = 4 * len(regs)
            if not regs:
                regs, size = [15], 0x40
            L = ["a = r[13]", "w = WT32[a >> 24]"]
            for i in regs:
                L.append("v = R32[a >> 24](a)")
                L.append("cpu.pc = v & 0xFFFFFFFE" if i == 15 else f"r[{i}] = v")
                L.append("a = (a + 4) & M")
            L.append(f"r[13] = (r[13] + {size}) & M")
        else:
            if extra:
                regs.append(14)
            size = 4 * len(regs)
            if not regs:
                regs, size = [15], 0x40
            L = [f"a = (r[13] - {size}) & M", "w = WT32[a >> 24]"]
            for i in regs:
                val = "r[15] + 2" if i == 15 else f"r[{i}]"
                L += [f"W32[a >> 24](a, {val})", "a = (a + 4) & M"]
            L.append(f"r[13] = (r[13] - {size}) & M")
        L.append(f"return {len(regs) + (2 if load else 0)} + {len(regs)} * w")
        return self._build(L)

    def _t_ldm_stm(self, op: int):
        load = bool(op & 0x0800)
        rb = (op >> 8) & 7
        regs = [i for i in range(8) if op >> i & 1]
        size = 4 * len(regs)
        if not regs:
            regs, size = [15], 0x40
        new = f"(base + {size}) & M"
        L = [f"base = r[{rb}]", "a = base", "w = WT32[a >> 24]"]
        if load:
            if rb not in regs:
                L.append(f"r[{rb}] = {new}")
            for i in regs:
                L.append("v = R32[a >> 24](a)")
                L.append("cpu.pc = v & 0xFFFFFFFE" if i == 15 else f"r[{i}] = v")
                L.append("a = (a + 4) & M")
        else:
            first = regs[0]
            for i in regs:
                if i == 15:
                    val = "r[15] + 2"
                elif i == rb and i != first:
                    val = new
                else:
                    val = f"r[{i}]"
                L += [f"W32[a >> 24](a, {val})", "a = (a + 4) & M"]
            L.append(f"r[{rb}] = {new}")
        L.append(f"return {len(regs) + (2 if load else 0)} + {len(regs)} * w")
        return self._build(L)
