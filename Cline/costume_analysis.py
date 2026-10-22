"""Comprehensive analysis of costume code at 0x10017b."""
import struct
from pathlib import Path
from capstone import Cs, CS_ARCH_X86, CS_MODE_64

SELF_PATH = Path(r'C:\Games\gow3-windows\Rom\eboot.bin')
data = SELF_PATH.read_bytes()

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

def disassemble(vaddr, size=256):
    """Disassemble code at virtual address."""
    code = read_bytes(vaddr, size)
    md = Cs(CS_ARCH_X86, CS_MODE_64)
    result = []
    for insn in md.disasm(code, vaddr):
        result.append((insn.address, insn.mnemonic, insn.op_str, insn.bytes))
    return result

print("="*70)
print("COSTUME CODE ANALYSIS AT 0x10017b")
print("="*70)

insns = disassemble(0x10017b, 128)
for addr, mnem, op_str, bytes_hex in insns[:20]:
    hex_str = ' '.join(f'{b:02x}' for b in bytes_hex)
    print(f"0x{addr:06x}: {hex_str:<30s} {mnem:8s} {op_str}")

print("\n" + "="*70)
print("ADDRESS VERIFICATION")
print("="*70)

# Verify each address calculation
print("\n1. Selection variable (0x10017b):")
print("   lea rax, [rip + 0x2c0faa]")
rip = 0x100182  # next instruction
target = rip + 0x2c0faa
print(f"   rip = 0x{rip:x}")
print(f"   target = 0x{rip:x} + 0x2c0faa = 0x{target:x}")
print(f"   File offset: {vaddr_to_file(target)}")
if vaddr_to_file(target):
    val = struct.unpack('<I', read_bytes(target, 4))[0]
    print(f"   Initial value: {val} (0x{val:x})")

print("\n2. Costume table (0x100186):")
print("   lea rcx, [rip + 0x31e9a7]")
rip = 0x10018d
target = rip + 0x31e9a7
print(f"   rip = 0x{rip:x}")
print(f"   target = 0x{rip:x} + 0x31e9a7 = 0x{target:x}")
print(f"   File offset: {vaddr_to_file(target)}")
if vaddr_to_file(target) is None:
    print("   Location: BSS (zero-initialized)")

print("\n3. Format string (0x100196):")
print("   lea rdi, [rip + 0x35f8c3]")
rip = 0x10019d
target = rip + 0x35f8c3
print(f"   rip = 0x{rip:x}")
print(f"   target = 0x{rip:x} + 0x35f8c3 = 0x{target:x}")
fmt_bytes = read_bytes(target, 32)
fmt_str = fmt_bytes.split(b'\x00')[0].decode('ascii', errors='replace')
print(f"   String: '{fmt_str}'")

print("\n4. R_Hero%02d string (0x10019d):")
print("   lea rdx, [rip + 0x24f6fc]")
rip = 0x1001a4
target = rip + 0x24f6fc
print(f"   rip = 0x{rip:x}")
print(f"   target = 0x{rip:x} + 0x24f6fc = 0x{target:x}")
hero_bytes = read_bytes(target, 32)
hero_str = hero_bytes.split(b'\x00')[0].decode('ascii', errors='replace')
print(f"   String: '{hero_str}'")

print("\n" + "="*70)
print("SEARCHING FOR OTHER REFERENCES TO KEY ADDRESSES")
print("="*70)

# Search for references to the selection variable and table
selection_addr = 0x100182 + 0x2c0faa  # 0x3c112c
table_addr = 0x10018d + 0x31e9a7     # 0x41eb34

print(f"\nSelection variable: 0x{selection_addr:x}")
print(f"Costume table: 0x{table_addr:x}")

# Search for references to these addresses
code_start = 0x4000
code_end = 0x4000 + 0x3bd43c

for name, target in [("selection", selection_addr), ("table", table_addr)]:
    print(f"\nSearching for references to {name} (0x{target:x})...")
    refs = []
    for offset in range(code_start, code_end - 7):
        if data[offset] in [0x48, 0x49, 0x4a, 0x4b, 0x4c, 0x4d, 0x4e, 0x4f]:
            if data[offset + 1] == 0x8d:
                modrm = data[offset + 2]
                if (modrm & 0xc7) == 0x05:
                    disp = struct.unpack('<i', data[offset+3:offset+7])[0]
                    rip = (offset - 0x4000) + 7
                    calc_target = rip + disp
                    if calc_target == target:
                        vaddr = offset - 0x4000
                        refs.append(vaddr)
    
    print(f"Found {len(refs)} references:")
    for ref in refs[:10]:
        print(f"  0x{ref:x}")

print("\n" + "="*70)
print("ANALYZING FUNCTION BOUNDARIES")
print("="*70)

# Look backwards from 0x10017b to find function start
print("\nSearching backwards for function prologue...")
for offset in range(0x10017b, max(0x10017b - 512, 0), -1):
    # Look for common function prologues
    if data[offset:offset+3] == b'\x55\x48\x89':  # push rbp; mov rbp, rsp
        print(f"Possible function start at 0x{offset:x}")
        insns = disassemble(offset, 128)
        if insns:
            addr, mnem, op_str, _ = insns[0]
            print(f"  0x{addr:06x}: {mnem} {op_str}")
        break
