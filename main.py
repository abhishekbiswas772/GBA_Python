import sys
from src.gameboy.cartridge import Cartridge
from src.gameboy.mmu import MMU


def main() -> None:
    if len(sys.argv) < 2:
        sys.exit("usage: python main.py <path of rom.gb>")
    
    cart = Cartridge.from_file(sys.argv[1])
    mmu = MMU(cart)
    h = cart.header

    print(cart)
    print(f"  logo ok: {h.logo_ok}   checksum ok: {h.checksum_ok}")
    if h.rom_banks > 2:
        print("  NOTE: >32 KiB ROM - only banks 0-1 visible until MBC support (Phase 7)")

    # Read through the MMU, exactly like the CPU will in Phase 2
    first4 = [mmu.read(0x0100 + i) for i in range(4)]
    print("  bytes @0x0100:", " ".join(f"{b:02X}" for b in first4))
    if first4[1] == 0xC3:                       # JP nn
        print(f"  -> NOP; JP 0x{mmu.read_word(0x0102):04X}   (game code starts there)")

    mmu.write(0xC000, 0x42)
    print(f"  WRAM 0xC000 = 0x{mmu.read(0xC000):02X}, echo 0xE000 = 0x{mmu.read(0xE000):02X}")


if __name__ == "__main__":
    main()