from src.gameboy.cartridge import Cartridge
from src.gameboy.common import IO_READ_OR as _IO_OR


class MMU:
    def __init__(self, cartridge: Cartridge):
        self.cart = cartridge
        self.vram = bytearray(0x2000)
        self.wram = bytearray(0x2000)
        self.oam = bytearray(0xA0)
        self.io = bytearray(0x80)
        self.hram = bytearray(0x7F)
        self.ie = 0x00
        self.timer = None
        self.ppu = None
        self.joypad = None
        self.apu = None

        self.serial_output = []


    def read(self, addr: int) -> int:
        addr &= 0xFFFF
        if addr < 0x8000:
            return self.cart.read(addr)
        if addr < 0xA000:
            if self.ppu is not None and self.ppu.vram_locked:
                return 0xFF
            return self.vram[addr - 0x8000]
        if addr < 0xC000:
            return self.cart.read(addr)
        if addr < 0xE000:
            return self.wram[addr - 0xC000]
        if addr < 0xFE00:
            return self.wram[addr - 0xE000]          
        if addr < 0xFEA0:
            if self.ppu is not None and self.ppu.oam_locked:
                return 0xFF
            return self.oam[addr - 0xFE00]
        if addr < 0xFF00:
            return 0xFF                              
        if addr < 0xFF80:
            return self.read_io(addr)
        if addr < 0xFFFF:
            return self.hram[addr - 0xFF80]
        return self.ie

    def write(self, addr: int, value: int) -> None:
        addr &= 0xFFFF
        value &= 0xFF
        if addr < 0x8000:
            self.cart.write(addr, value)             
        elif addr < 0xA000:
            if self.ppu is None or not self.ppu.vram_locked:
                self.vram[addr - 0x8000] = value
        elif addr < 0xC000:
            self.cart.write(addr, value)
        elif addr < 0xE000:
            self.wram[addr - 0xC000] = value
        elif addr < 0xFE00:
            self.wram[addr - 0xE000] = value         
        elif addr < 0xFEA0:
            if self.ppu is None or not self.ppu.oam_locked:
                self.oam[addr - 0xFE00] = value
        elif addr < 0xFF00:
            pass                                     
        elif addr < 0xFF80:
            self.write_io(addr, value)
        elif addr < 0xFFFF:
            self.hram[addr - 0xFF80] = value
        else:
            self.ie = value
        
    def read_word(self, addr: int) -> int:
        lo = self.read(addr)
        hi = self.read(addr + 1)
        return (hi << 8) | lo

    def write_word(self, addr: int , value : int) -> None:
        self.write(addr, value & 0xFF)
        self.write(addr + 1, (value >> 8) & 0xFF)

    def request_interrupt(self, bit: int) -> None:
        self.io[0x0F] |= bit

    def read_io(self, addr: int) -> int:
        if addr == 0xFF00 and self.joypad is not None:
            return self.joypad.read()
        if 0xFF10 <= addr <= 0xFF3F and self.apu is not None:
            return self.apu.read(addr)
        if 0xFF04 <= addr <= 0xFF07 and self.timer is not None:
            return self.timer.read(addr)
        if 0xFF40 <= addr <= 0xFF4B and self.ppu is not None:
            return self.ppu.read(addr)
        i = addr - 0xFF00
        return self.io[i] | _IO_OR[i]

    def write_io(self, addr: int, value: int) -> None:
        if addr == 0xFF00 and self.joypad is not None:
            self.joypad.write(value)
            return
        if 0xFF10 <= addr <= 0xFF3F and self.apu is not None:
            self.apu.write(addr, value)
            return
        if 0xFF04 <= addr <= 0xFF07 and self.timer is not None:
            self.timer.write(addr, value)
            return
        if 0xFF40 <= addr <= 0xFF4B and self.ppu is not None:
            self.ppu.write(addr, value)
            return
        if addr == 0xFF0F:
            value &= 0x1F
        self.io[addr - 0xFF00] = value
        if addr == 0xFF02 and value & 0x81 == 0x81:
            # Internal-clock transfer with no link partner: completes at once,
            # shifting in 1s (SB=0xFF), clearing SC bit 7 and raising the serial IRQ.
            if len(self.serial_output) < 65536:
                self.serial_output.append(chr(self.io[0x01]))
            self.io[0x01] = 0xFF
            self.io[0x02] = value & 0x7F
            self.request_interrupt(0x08)

    def dma_transfer(self, src: int) -> None:
        oam = self.oam
        for i in range(160):
            a = (src + i) & 0xFFFF
            if a < 0x8000:
                v = self.cart.read(a)
            elif a < 0xA000:
                v = self.vram[a - 0x8000]
            elif a < 0xC000:
                v = self.cart.read(a)
            elif a < 0xE000:
                v = self.wram[a - 0xC000]
            else:                        # E000-FFFF: DMA source mirrors WRAM
                v = self.wram[(a - 0xE000) & 0x1FFF]
            oam[i] = v
    
    def serial_text(self) -> str:
        return "".join(self.serial_output)
        