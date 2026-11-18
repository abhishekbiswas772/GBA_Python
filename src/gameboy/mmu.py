from src.gameboy.cartridge import Cartridge


class MMU:
    def __init__(self, cartridge: Cartridge):
        self.cart = cartridge
        self.vram = bytearray(0x2000)
        self.wram = bytearray(0x2000)
        self.oam = bytearray(0xA0)
        self.io = bytearray(0x80)
        self.hram = bytearray(0x7F)
        self.ie = 0x00

        self.serial_output = []


    def read(self, addr: int) -> int:
        addr &= 0xFFFF
        if addr < 0x8000:
            return self.cart.read(addr)
        if addr < 0xA000:
            return self.vram[addr - 0x8000]
        if addr < 0xC000:
            return self.cart.read(addr)
        if addr < 0xE000:
            return self.wram[addr - 0xC000]
        if addr < 0xFE00:
            return self.wram[addr - 0xE000]          
        if addr < 0xFEA0:
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
            self.vram[addr - 0x8000] = value
        elif addr < 0xC000:
            self.cart.write(addr, value)
        elif addr < 0xE000:
            self.wram[addr - 0xC000] = value
        elif addr < 0xFE00:
            self.wram[addr - 0xE000] = value         
        elif addr < 0xFEA0:
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

    def read_io(self, addr: int) -> int:
        if addr == 0xFF44:
            return 0x90
        return self.io[addr - 0xFF00]

    def write_io(self, addr: int, value: int) -> None:
        self.io[addr - 0xFF00] = value 
        if addr == 0xFF02 and value == 0x81:
            self.serial_output.append(chr(self.io[0x01]))
            self.io[0x02] = 0x01
    
    def serial_text(self) -> str:
        return "".join(self.serial_output)
        