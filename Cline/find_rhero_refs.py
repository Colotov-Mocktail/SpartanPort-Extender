"""Search for code that references the R_Hero%02d string."""
import struct
from pathlib import Path
from capstone import Cs, CS_ARCH_X86, CS_MODE_64

SELF_PATH = Path(r'C:\Games\gow3-windows\Rom\eboot.bin')
data = SELF_PATH.read_bytes()

# R_Hero%02d is at vaddr 0x34f8a0
STRING_ADDR = 0x34f8a0

def vaddr_to_file(vaddr):
    """Convert virtual address to file offset."""
    if 0x0 <= vaddr < 0x3bd43c:
        return 0x4000 + vaddr
    if 0x3c0000 <= vaddr < 0x3c0000 + 0x498e8:
        return 0x3c4000 + (vaddr - 0x3c0000)
    return None

def read_bytes(vaddr, size):
    """Read bytes from virtual address."""
    offset = vaddr_to_file(vaddr)
    if offset is None:
        return b'\x00' * size
    return data[offset:offset+size]

def disassemble(vaddr, size=128):
    """Disassemble code at virtual address."""
    code = read_bytes(vaddr, size)
    md = Cs(CS_ARCH_X86, CS_MODE_64)
    result = []
    for insn in md.disasm(code, vaddr):
        result.append((insn.address, insn.mnemonic, insn.op_str, insn.bytes))
    return result

print("="*70)
print(f"SEARCHING FOR REFERENCES TO R_Hero%02d AT 0x{STRING_ADDR:x}")
print("="*70)

# Search for RIP-relative references to STRING_ADDR
# Look for: lea reg, [rip + disp32] where rip + disp32 = STRING_ADDR
# The displacement is calculated as: disp32 = STRING_ADDR - rip
# where rip is the address of the next instruction after the lea

# Search in code segment
code_start_file = 0x4000
code_end_file = 0x4000 + 0x3bd43c

print("\nSearching for lea instructions that reference the string...")
print("This may take a moment...\n")

# Simple heuristic search: look for the displacement bytes
# For a lea at address X, the displacement is at X+3 (after REX + opcode + modrm)
# and rip = X + 7 (length of lea instruction)
# So disp32 = STRING_ADDR - (X + 7)

found_refs = []

# Search every possible lea instruction
for offset in range(code_start_file, code_end_file - 7):
    # Check for REX prefix (0x48, 0x4c, etc.)
    if data[offset] in [0x48, 0x49, 0x4a, 0x4b, 0x4c, 0x4d, 0x4e, 0x4f]:
        # Check for lea opcode (0x8d)
        if data[offset + 1] == 0x8d:
            # Check for RIP-relative modrm (mod=00, rm=101)
            modrm = data[offset + 2]
            if (modrm & 0xc7) == 0x05:  # mod=00, rm=101
                # Extract displacement
                disp = struct.unpack('<i', data[offset+3:offset+7])[0]
                rip = (offset - 0x4000) + 7  # Convert to vaddr and add 7
                target = rip + disp
                if target == STRING_ADDR:
                    vaddr = offset - 0x4000
                    found_refs.append(vaddr)

print(f"Found {len(found_refs)} references to R_Hero%02d:\n")

# Disassemble each reference
for vaddr in found_refs:
    print(f"Reference at vaddr 0x{vaddr:x}:")
    insns = disassemble(vaddr, 64)
    for addr, mnem, op_str, bytes_hex in insns[:8]:
        hex_str = ' '.join(f'{b:02x}' for b in bytes_hex)
        print(f"  0x{addr:06x}: {hex_str:<30s} {mnem:8s} {op_str}")
    print()

if len(found_refs) > 0:
    print("="*70)
    print("ANALYZING CONTEXT AROUND FIRST REFERENCE")
    print("="*70)
    
    # Disassemble more context around the first reference
    first_ref = found_refs[0]
    print(f"\nDisassembly starting 64 bytes before first reference (0x{first_ref-64:x}):\n")
    insns = disassemble(first_ref - 64, 256)
    for addr, mnem, op_str, bytes_hex in insns[:40]:
        hex_str = ' '.join(f'{b:02x}' for b in bytes_hex)
        marker = " <-- R_Hero%02d" if addr == first_ref else ""
        print(f"0x{addr:06x}: {hex_str:<30s} {mnem:8s} {op_str}{marker}")
