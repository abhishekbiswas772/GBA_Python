from src.gameboy.common import (
    FLAG_C, FLAG_H, FLAG_N, FLAG_Z, 
    R8, R16, R16_STACK,
    signed8
)


class CPU:
    def __init__(self, mmu):
        self.mmu = mmu
        self.a, self.f = 0x01, 0xB0
        self.b, self.c = 0x00, 0x13
        self.d, self.e = 0x00, 0xD8
        self.h, self.l = 0x01, 0x4D
        self.sp = 0xFFFE
        self.pc = 0x0100

        self.ime = False            
        self.ime_pending = False    
        self.halted = False

        self.ops = [self._decode(op) for op in range(256)]        
        self.cb_ops = [self._decode_cb(op) for op in range(256)]  

    def step(self) -> int:
        if self.halted:
            if self.mmu.read(0xFFFF) & self.mmu.read(0xFF0F) & 0x1F:
                self.halted = False
            else:
                return 4

        enable_ime_after = self.ime_pending
        opcode = self.fetch8()
        cycles = self.ops[opcode]()
        if enable_ime_after and self.ime_pending:
            self.ime = True
            self.ime_pending = False
        return cycles


    def fetch8(self) -> int:
        value = self.mmu.read(self.pc)
        self.pc = (self.pc + 1) & 0xFFFF
        return value

    def fetch16(self) -> int:
        lo = self.fetch8()
        hi = self.fetch8()
        return (hi << 8) | lo

    @property
    def af(self): return (self.a << 8) | self.f
    @af.setter
    def af(self, v): self.a, self.f = (v >> 8) & 0xFF, v & 0xF0   # low 4 bits of F are always 0

    @property
    def bc(self): return (self.b << 8) | self.c
    @bc.setter
    def bc(self, v): self.b, self.c = (v >> 8) & 0xFF, v & 0xFF

    @property
    def de(self): return (self.d << 8) | self.e
    @de.setter
    def de(self, v): self.d, self.e = (v >> 8) & 0xFF, v & 0xFF

    @property
    def hl(self): return (self.h << 8) | self.l
    @hl.setter
    def hl(self, v): self.h, self.l = (v >> 8) & 0xFF, v & 0xFF

    def get_r8(self, i: int) -> int:
        if i == 6:
            return self.mmu.read(self.hl)
        return getattr(self, R8[i])

    def set_r8(self, i: int, v: int) -> None:
        if i == 6:
            self.mmu.write(self.hl, v)
        else:
            setattr(self, R8[i], v & 0xFF)

    def get_r16(self, name: str) -> int:
        return getattr(self, name)

    def set_r16(self, name: str, v: int) -> None:
        setattr(self, name, v & 0xFFFF)

    def set_flags(self, z, n, h, c) -> None:
        f = self.f
        for flag, val in ((FLAG_Z, z), (FLAG_N, n), (FLAG_H, h), (FLAG_C, c)):
            if val is not None:
                f = (f | flag) if val else (f & ~flag)
        self.f = f & 0xF0

    def flag(self, mask: int) -> bool:
        return bool(self.f & mask)

    def condition(self, i: int) -> bool:
        return (not self.flag(FLAG_Z), self.flag(FLAG_Z),
                not self.flag(FLAG_C), self.flag(FLAG_C))[i]

    def push16(self, v: int) -> None:
        self.sp = (self.sp - 1) & 0xFFFF
        self.mmu.write(self.sp, (v >> 8) & 0xFF)   
        self.sp = (self.sp - 1) & 0xFFFF
        self.mmu.write(self.sp, v & 0xFF)

    def pop16(self) -> int:
        lo = self.mmu.read(self.sp)
        self.sp = (self.sp + 1) & 0xFFFF
        hi = self.mmu.read(self.sp)
        self.sp = (self.sp + 1) & 0xFFFF
        return (hi << 8) | lo


    def alu(self, op: int, v: int) -> None:
        """op = y field: 0 ADD, 1 ADC, 2 SUB, 3 SBC, 4 AND, 5 XOR, 6 OR, 7 CP"""
        a = self.a
        carry = 1 if self.flag(FLAG_C) else 0
        if op == 0 or op == 1:                          
            cin = carry if op == 1 else 0
            r = a + v + cin
            self.set_flags((r & 0xFF) == 0, False, (a & 0xF) + (v & 0xF) + cin > 0xF, r > 0xFF)
            self.a = r & 0xFF
        elif op == 2 or op == 3 or op == 7:             
            cin = carry if op == 3 else 0
            r = a - v - cin
            self.set_flags((r & 0xFF) == 0, True, (a & 0xF) - (v & 0xF) - cin < 0, r < 0)
            if op != 7:                                
                self.a = r & 0xFF
        elif op == 4:                                  
            self.a = a & v
            self.set_flags(self.a == 0, False, True, False)
        elif op == 5:                                  
            self.a = a ^ v
            self.set_flags(self.a == 0, False, False, False)
        else:                                           
            self.a = a | v
            self.set_flags(self.a == 0, False, False, False)


    def rotate_shift(self, op: int, v: int) -> int:
        carry = 1 if self.flag(FLAG_C) else 0
        if op == 0:   out, r = v >> 7, ((v << 1) | (v >> 7)) & 0xFF        
        elif op == 1: out, r = v & 1, (v >> 1) | ((v & 1) << 7)            
        elif op == 2: out, r = v >> 7, ((v << 1) | carry) & 0xFF           
        elif op == 3: out, r = v & 1, (v >> 1) | (carry << 7)              
        elif op == 4: out, r = v >> 7, (v << 1) & 0xFF                     
        elif op == 5: out, r = v & 1, (v >> 1) | (v & 0x80)                
        elif op == 6: out, r = 0, ((v << 4) | (v >> 4)) & 0xFF             
        else:         out, r = v & 1, v >> 1                               
        self.set_flags(r == 0, False, False, out == 1)
        return r

   
    def _decode(self, op: int):
        x, y, z = op >> 6, (op >> 3) & 7, op & 7
        p, q = y >> 1, y & 1

        if x == 1:
            if op == 0x76:
                def halt():
                    self.halted = True
                    return 4
                return halt

            def ld_r_r():
                self.set_r8(y, self.get_r8(z))
                return 8 if (y == 6 or z == 6) else 4
            return ld_r_r

        if x == 2:
            def alu_r():
                self.alu(y, self.get_r8(z))
                return 8 if z == 6 else 4
            return alu_r

        if x == 0:
            if z == 0:
                if y == 0:
                    return lambda: 4                                   
                if y == 1:
                    def ld_a16_sp():                                   
                        self.mmu.write_word(self.fetch16(), self.sp)
                        return 20
                    return ld_a16_sp
                if y == 2:
                    def stop():                                        
                        self.fetch8()
                        return 4
                    return stop
                if y == 3:
                    def jr():                                          
                        e = signed8(self.fetch8())
                        self.pc = (self.pc + e) & 0xFFFF
                        return 12
                    return jr

                def jr_cc():                                          
                    e = signed8(self.fetch8())
                    if self.condition(y - 4):
                        self.pc = (self.pc + e) & 0xFFFF
                        return 12
                    return 8
                return jr_cc

            if z == 1:
                rr = R16[p]
                if q == 0:
                    def ld_rr_d16():                                   
                        self.set_r16(rr, self.fetch16())
                        return 12
                    return ld_rr_d16

                def add_hl_rr():                                       
                    hl, v = self.hl, self.get_r16(rr)
                    r = hl + v
                    self.set_flags(None, False, (hl & 0xFFF) + (v & 0xFFF) > 0xFFF, r > 0xFFFF)
                    self.hl = r & 0xFFFF
                    return 8
                return add_hl_rr

            if z == 2:
                def indirect_addr():                                   
                    if p == 0: return self.bc
                    if p == 1: return self.de
                    addr = self.hl
                    self.hl = (addr + 1) & 0xFFFF if p == 2 else (addr - 1) & 0xFFFF
                    return addr

                if q == 0:
                    def ld_ind_a():                                    
                        self.mmu.write(indirect_addr(), self.a)
                        return 8
                    return ld_ind_a

                def ld_a_ind():                                        
                    self.a = self.mmu.read(indirect_addr())
                    return 8
                return ld_a_ind

            if z == 3:
                rr = R16[p]
                delta = 1 if q == 0 else -1

                def inc_dec_rr():                                      
                    self.set_r16(rr, self.get_r16(rr) + delta)
                    return 8
                return inc_dec_rr

            if z == 4:
                def inc_r():                                           
                    v = self.get_r8(y)
                    r = (v + 1) & 0xFF
                    self.set_r8(y, r)
                    self.set_flags(r == 0, False, (v & 0xF) == 0xF, None)
                    return 12 if y == 6 else 4
                return inc_r

            if z == 5:
                def dec_r():                                          
                    v = self.get_r8(y)
                    r = (v - 1) & 0xFF
                    self.set_r8(y, r)
                    self.set_flags(r == 0, True, (v & 0xF) == 0, None)
                    return 12 if y == 6 else 4
                return dec_r

            if z == 6:
                def ld_r_d8():                                        
                    self.set_r8(y, self.fetch8())
                    return 12 if y == 6 else 8
                return ld_r_d8

            return [self._rlca, self._rrca, self._rla, self._rra,
                    self._daa, self._cpl, self._scf, self._ccf][y]

        if z == 0:
            if y < 4:
                def ret_cc():                                          
                    if self.condition(y):
                        self.pc = self.pop16()
                        return 20
                    return 8
                return ret_cc
            if y == 4:
                def ldh_a8_a():                                        
                    self.mmu.write(0xFF00 + self.fetch8(), self.a)
                    return 12
                return ldh_a8_a
            if y == 6:
                def ldh_a_a8():                                        
                    self.a = self.mmu.read(0xFF00 + self.fetch8())
                    return 12
                return ldh_a_a8

            def sp_plus_e():                                
                raw = self.fetch8()
                sp = self.sp
                r = (sp + signed8(raw)) & 0xFFFF
                half = (sp & 0xF) + (raw & 0xF) > 0xF
                carry = (sp & 0xFF) + raw > 0xFF
                self.set_flags(False, False, half, carry)
                if y == 5:
                    self.sp = r
                    return 16
                self.hl = r
                return 12
            return sp_plus_e

        if z == 1:
            if q == 0:
                rr = R16_STACK[p]

                def pop():                                             
                    self.set_r16(rr, self.pop16())
                    return 12
                return pop
            if p == 0:
                def ret():                                             
                    self.pc = self.pop16()
                    return 16
                return ret
            if p == 1:
                def reti():                                            
                    self.pc = self.pop16()
                    self.ime = True
                    return 16
                return reti
            if p == 2:
                def jp_hl():                                           
                    self.pc = self.hl
                    return 4
                return jp_hl

            def ld_sp_hl():                                            
                self.sp = self.hl
                return 8
            return ld_sp_hl

        if z == 2:
            if y < 4:
                def jp_cc():                                           
                    addr = self.fetch16()
                    if self.condition(y):
                        self.pc = addr
                        return 16
                    return 12
                return jp_cc
            if y == 4:
                def ld_c_a():                                          
                    self.mmu.write(0xFF00 + self.c, self.a)
                    return 8
                return ld_c_a
            if y == 5:
                def ld_a16_a():                                        
                    self.mmu.write(self.fetch16(), self.a)
                    return 16
                return ld_a16_a
            if y == 6:
                def ld_a_c():                                          
                    self.a = self.mmu.read(0xFF00 + self.c)
                    return 8
                return ld_a_c

            def ld_a_a16():                                            
                self.a = self.mmu.read(self.fetch16())
                return 16
            return ld_a_a16

        if z == 3:
            if y == 0:
                def jp():                                             
                    self.pc = self.fetch16()
                    return 16
                return jp
            if y == 1:
                def prefix_cb():                                       
                    return self.cb_ops[self.fetch8()]()
                return prefix_cb
            if y == 6:
                def di():                                             
                    self.ime = False
                    self.ime_pending = False
                    return 4
                return di
            if y == 7:
                def ei():                                              
                    self.ime_pending = True
                    return 4
                return ei
            return self._illegal(op)

        if z == 4:
            if y < 4:
                def call_cc():                                         
                    addr = self.fetch16()
                    if self.condition(y):
                        self.push16(self.pc)
                        self.pc = addr
                        return 24
                    return 12
                return call_cc
            return self._illegal(op)

        if z == 5:
            if q == 0:
                rr = R16_STACK[p]

                def push():                                            
                    self.push16(self.get_r16(rr))
                    return 16
                return push
            if p == 0:
                def call():                                            
                    addr = self.fetch16()
                    self.push16(self.pc)
                    self.pc = addr
                    return 24
                return call
            return self._illegal(op)

        if z == 6:
            def alu_d8():                                              
                self.alu(y, self.fetch8())
                return 8
            return alu_d8

        # z == 7
        def rst():                                                     
            self.push16(self.pc)
            self.pc = y * 8
            return 16
        return rst

    def _decode_cb(self, op: int):
        x, y, z = op >> 6, (op >> 3) & 7, op & 7
        hl = z == 6

        if x == 0:
            def rot():                                      
                self.set_r8(z, self.rotate_shift(y, self.get_r8(z)))
                return 16 if hl else 8
            return rot
        if x == 1:
            def bit():                                                 
                is_zero = not (self.get_r8(z) >> y) & 1
                self.set_flags(is_zero, False, True, None)
                return 12 if hl else 8
            return bit
        if x == 2:
            def res():                                                 
                self.set_r8(z, self.get_r8(z) & ~(1 << y))
                return 16 if hl else 8
            return res

        def set_():                                                    
            self.set_r8(z, self.get_r8(z) | (1 << y))
            return 16 if hl else 8
        return set_

    def _illegal(self, op: int):
        def illegal():
            raise RuntimeError(f"Illegal opcode 0x{op:02X} at 0x{(self.pc - 1) & 0xFFFF:04X}")
        return illegal

    def _rlca(self):
        a = self.a
        self.a = ((a << 1) | (a >> 7)) & 0xFF
        self.set_flags(False, False, False, a >> 7)
        return 4

    def _rrca(self):
        a = self.a
        self.a = (a >> 1) | ((a & 1) << 7)
        self.set_flags(False, False, False, a & 1)
        return 4

    def _rla(self):
        a, carry = self.a, 1 if self.flag(FLAG_C) else 0
        self.a = ((a << 1) | carry) & 0xFF
        self.set_flags(False, False, False, a >> 7)
        return 4

    def _rra(self):
        a, carry = self.a, 1 if self.flag(FLAG_C) else 0
        self.a = (a >> 1) | (carry << 7)
        self.set_flags(False, False, False, a & 1)
        return 4

    def _daa(self):
        a = self.a
        n, h, c = self.flag(FLAG_N), self.flag(FLAG_H), self.flag(FLAG_C)
        adjust = 0
        if not n:
            if h or (a & 0x0F) > 0x09:
                adjust |= 0x06
            if c or a > 0x99:
                adjust |= 0x60
                c = True
            a = (a + adjust) & 0xFF
        else:
            if h:
                adjust |= 0x06
            if c:
                adjust |= 0x60
            a = (a - adjust) & 0xFF
        self.a = a
        self.set_flags(a == 0, None, False, c)
        return 4

    def _cpl(self):
        self.a ^= 0xFF
        self.set_flags(None, True, True, None)
        return 4

    def _scf(self):
        self.set_flags(None, False, False, True)
        return 4

    def _ccf(self):
        self.set_flags(None, False, False, not self.flag(FLAG_C))
        return 4

    def doctor_line(self) -> str:
        mem = ",".join(f"{self.mmu.read((self.pc + i) & 0xFFFF):02X}" for i in range(4))
        return (f"A:{self.a:02X} F:{self.f:02X} B:{self.b:02X} C:{self.c:02X} "
                f"D:{self.d:02X} E:{self.e:02X} H:{self.h:02X} L:{self.l:02X} "
                f"SP:{self.sp:04X} PC:{self.pc:04X} PCMEM:{mem}")