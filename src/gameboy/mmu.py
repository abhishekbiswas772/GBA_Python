from src.gameboy.cartridge import Cartridge
from src.gameboy.common import IO_READ_OR as _IO_OR

HDMA_BLOCK_CYCLES = 32   # real-time T-cycles the CPU is stalled per 16-byte HDMA block


class MMU:
    def __init__(self, cartridge: Cartridge, cgb: bool = False):
        self.cart = cartridge
        self.cgb = cgb
        self.vram = bytearray(0x4000)       # 2 banks (bank 1 used in CGB mode only)
        self.wram = bytearray(0x8000)       # 8 x 4 KiB banks (banks 2-7 CGB only)
        self.oam = bytearray(0xA0)
        self.io = bytearray(0x80)
        self.hram = bytearray(0x7F)
        self.ie = 0x00
        self.timer = None
        self.ppu = None
        self.joypad = None
        self.apu = None

        self.serial_output = []

        # CGB state
        self.vram_off = 0                   # VBK * 0x2000
        self.wram_hi = 0x1000               # SVBK bank * 0x1000, mapped at D000-DFFF
        self.svbk = 0
        self.double_speed = 0               # KEY1 bit 7 (0/1, used as a shift)
        self.key1_prepare = 0
        self.stall = 0                      # CPU cycles the CPU is stalled for (HDMA)
        self.hdma_src = 0
        self.hdma_dst = 0
        self.hdma_active = False            # HBlank DMA in progress
        self.hdma_blocks = 0                # 16-byte blocks remaining
        self.hdma_reg = 0xFF                # FF55 read-back
        self.opri = 0
        self.rp = 0
        self.undoc = bytearray(4)           # FF72-FF75

    def read(self, addr: int) -> int:
        addr &= 0xFFFF
        if addr < 0x8000:
            return self.cart.read(addr)
        if addr < 0xA000:
            if self.ppu is not None and self.ppu.vram_locked:
                return 0xFF
            return self.vram[self.vram_off + addr - 0x8000]
        if addr < 0xC000:
            return self.cart.read(addr)
        if addr < 0xD000:
            return self.wram[addr - 0xC000]
        if addr < 0xE000:
            return self.wram[self.wram_hi + addr - 0xD000]
        if addr < 0xF000:
            return self.wram[addr - 0xE000]
        if addr < 0xFE00:
            return self.wram[self.wram_hi + addr - 0xF000]
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
                self.vram[self.vram_off + addr - 0x8000] = value
        elif addr < 0xC000:
            self.cart.write(addr, value)
        elif addr < 0xD000:
            self.wram[addr - 0xC000] = value
        elif addr < 0xE000:
            self.wram[self.wram_hi + addr - 0xD000] = value
        elif addr < 0xF000:
            self.wram[addr - 0xE000] = value
        elif addr < 0xFE00:
            self.wram[self.wram_hi + addr - 0xF000] = value
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
        if self.cgb and addr >= 0xFF4C:
            return self._cgb_read(addr)
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
        if self.cgb and addr >= 0xFF4C:
            self._cgb_write(addr, value)
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

    # --- CGB registers (FF4C-FF7F) -----------------------------------------

    def _cgb_read(self, addr: int) -> int:
        if addr == 0xFF4D:
            return 0x7E | (self.double_speed << 7) | self.key1_prepare
        if addr == 0xFF4F:
            return 0xFE | (self.vram_off >> 13)
        if addr == 0xFF55:
            return self.hdma_reg
        if addr == 0xFF56:
            return (self.rp & 0xC1) | 0x3E          # bit 1 = 1: no IR light received
        if 0xFF68 <= addr <= 0xFF6B and self.ppu is not None:
            return self.ppu.read_cgb(addr)
        if addr == 0xFF6C:
            return 0xFE | self.opri
        if addr == 0xFF70:
            return 0xF8 | self.svbk
        if 0xFF72 <= addr <= 0xFF74:
            return self.undoc[addr - 0xFF72]
        if addr == 0xFF75:
            return self.undoc[3] | 0x8F
        return 0xFF

    def _cgb_write(self, addr: int, value: int) -> None:
        if addr == 0xFF4D:
            self.key1_prepare = value & 1
        elif addr == 0xFF4F:
            self.vram_off = (value & 1) << 13
        elif addr == 0xFF51:
            self.hdma_src = (self.hdma_src & 0x00FF) | (value << 8)
        elif addr == 0xFF52:
            self.hdma_src = (self.hdma_src & 0xFF00) | (value & 0xF0)
        elif addr == 0xFF53:
            self.hdma_dst = (self.hdma_dst & 0x00FF) | ((value & 0x1F) << 8)
        elif addr == 0xFF54:
            self.hdma_dst = (self.hdma_dst & 0xFF00) | (value & 0xF0)
        elif addr == 0xFF55:
            self._hdma_start(value)
        elif addr == 0xFF56:
            self.rp = value
        elif 0xFF68 <= addr <= 0xFF6B and self.ppu is not None:
            self.ppu.write_cgb(addr, value)
        elif addr == 0xFF6C:
            self.opri = value & 1
        elif addr == 0xFF70:
            self.svbk = value & 7
            self.wram_hi = (self.svbk or 1) << 12
        elif 0xFF72 <= addr <= 0xFF74:
            self.undoc[addr - 0xFF72] = value
        elif addr == 0xFF75:
            self.undoc[3] = value & 0x70

    def switch_speed(self) -> bool:
        """STOP with KEY1 armed toggles CPU double speed. Returns True if switched."""
        if not (self.cgb and self.key1_prepare):
            return False
        self.double_speed ^= 1
        self.key1_prepare = 0
        return True

    # --- VRAM DMA (HDMA) ----------------------------------------------------

    def _hdma_start(self, value: int) -> None:
        if self.hdma_active and not value & 0x80:
            self.hdma_active = False                 # cancel HBlank DMA
            self.hdma_reg = 0x80 | ((self.hdma_blocks - 1) & 0x7F)
            return
        blocks = (value & 0x7F) + 1
        if value & 0x80:                             # HBlank DMA: 16 bytes per HBlank
            self.hdma_active = True
            self.hdma_blocks = blocks
            self.hdma_reg = (blocks - 1) & 0x7F
        else:                                        # general purpose: all at once
            for _ in range(blocks):
                self._hdma_block()
            self.stall += (HDMA_BLOCK_CYCLES * blocks) << self.double_speed
            self.hdma_reg = 0xFF

    def hdma_hblank(self) -> None:
        """Called by the PPU on entering mode 0 of a visible line."""
        self._hdma_block()
        self.stall += HDMA_BLOCK_CYCLES << self.double_speed
        self.hdma_blocks -= 1
        if self.hdma_blocks <= 0:
            self.hdma_active = False
            self.hdma_reg = 0xFF
        else:
            self.hdma_reg = (self.hdma_blocks - 1) & 0x7F

    def _hdma_block(self) -> None:
        src, dst = self.hdma_src, self.hdma_dst
        vram, base = self.vram, self.vram_off
        for i in range(16):
            vram[base + ((dst + i) & 0x1FFF)] = self._dma_read((src + i) & 0xFFFF)
        self.hdma_src = (src + 16) & 0xFFFF
        self.hdma_dst = (dst + 16) & 0x1FFF

    # --- OAM DMA --------------------------------------------------------------

    def _dma_read(self, a: int) -> int:
        """Bus read for DMA sources: ignores PPU locks, E000-FFFF mirrors WRAM."""
        if a < 0x8000:
            return self.cart.read(a)
        if a < 0xA000:
            return self.vram[self.vram_off + a - 0x8000]
        if a < 0xC000:
            return self.cart.read(a)
        off = (a - 0xC000) & 0x1FFF
        if off < 0x1000:
            return self.wram[off]
        return self.wram[self.wram_hi + off - 0x1000]

    def dma_transfer(self, src: int) -> None:
        oam = self.oam
        rd = self._dma_read
        for i in range(160):
            oam[i] = rd((src + i) & 0xFFFF)

    def serial_text(self) -> str:
        return "".join(self.serial_output)
