"""Find all costume-related tables and bounds checking."""
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
print("ANALYZING BOUNDS CHECKING AT 0x1d55e")
print("="*70)

print("\nDisassembly around bounds check:")
insns = disassemble(0x1d540, 128)
for addr, mnem, op_str, bytes_hex in insns[:30]:
    hex_str = ' '.join(f'{b:02x}' for b in bytes_hex)
    print(f"0x{addr:06x}: {hex_str:<30s} {mnem:8s} {op_str}")

# Calculate the table address at 0x1d56b
print("\n" + "="*70)
print("TABLE ADDRESS CALCULATION")
print("="*70)

print("\nAt 0x1d56b: lea rcx, [rip + 0x40129e]")
rip = 0x1d572
table_addr = rip + 0x40129e
print(f"rip = 0x{rip:x}")
print(f"table address = 0x{rip:x} + 0x40129e = 0x{table_addr:x}")
print(f"File offset: {vaddr_to_file(table_addr)}")
if vaddr_to_file(table_addr) is None:
    print("Location: BSS (zero-initialized)")

print("\n" + "="*70)
print("SEARCHING FOR ALL TABLE ADDRESSES")
print("="*70)

# Search for all lea instructions that reference addresses in the BSS range
# BSS starts at 0x3c0000 + 0x498e8 = 0x4098e8
bss_start = 0x4098e8
bss_end = 0x3c0000 + 0x17874f8

code_start = 0x4000
code_end = 0x4000 + 0x3bd43c

tables = {}
print("\nSearching for lea instructions referencing BSS addresses...")

for offset in range(code_start, code_end - 7):
    if data[offset] in [0x48, 0x49, 0x4a, 0x4b, 0x4c, 0x4d, 0x4e, 0x4f]:
        if data[offset + 1] == 0x8d:
            modrm = data[offset + 2]
            if (modrm & 0xc7) == 0x05:
                disp = struct.unpack('<i', data[offset+3:offset+7])[0]
                rip = (offset - 0x4000) + 7
                target = rip + disp
                if bss_start <= target < bss_end:
                    vaddr = offset - 0x4000
                    if target not in tables:
                        tables[target] = []
                    tables[target].append(vaddr)

# Sort tables by address
sorted_tables = sorted(tables.items())

print(f"\nFound {len(sorted_tables)} unique BSS addresses referenced:")
for table_addr, refs in sorted_tables[:20]:
    print(f"  0x{table_addr:x}: {len(refs)} references")
    for ref in refs[:3]:
        print(f"    - 0x{ref:x}")

print("\n" + "="*70)
print("ANALYZING COSTUME CODE CONTEXT")
print("="*70)

# Look at the full function containing 0x10017b
print("\nSearching backwards from 0x10017b for function start...")
for offset in range(0x10017b, max(0x10017b - 1024, 0), -1):
    # Look for function prologue
    if data[offset:offset+4] == b'\x48\x83\xec':  # sub rsp, imm8
        print(f"Possible function start at 0x{offset:x}")
        insns = disassemble(offset, 512)
        print("\nFunction disassembly (first 50 instructions):")
        for addr, mnem, op_str, bytes_hex in insns[:50]:
            hex_str = ' '.join(f'{b:02x}' for b in bytes_hex)
            marker = ""
            if addr == 0x10017b:
                marker = " <-- COSTUME CODE"
            elif addr == 0x10019d:
                marker = " <-- R_Hero%02d"
            print(f"0x{addr:06x}: {hex_str:<30s} {mnem:8s} {op_str}{marker}")
        break

print("\n" + "="*70)
print("SUMMARY OF FINDINGS")
print("="*70)

print("""
Key addresses identified:
1. Selection variable: 0x3c112c (file-backed, stores current costume slot)
2. Costume mapping table: 0x41eb34 (BSS, indexed by selection to get archive ID)
3. Costume availability table: 0x41e810 (BSS, populated at runtime)
4. R_Hero%02d string: 0x34f8a0
5. Costume code: 0x10017b

Bounds checking:
- At 0x1d55e: checks if index <= 0x63 (99)
- This suggests the availability table has at least 100 entries
- The game likely supports up to 100 costume slots in theory

Costume loading logic:
1. Read selection from 0x3c112c
2. Index into table at 0x41eb34 to get archive ID
3. Store result back to 0x3c112c
4. Format as "R_Hero%02d" using the archive ID
5. Load the resulting WAD file

To add Master Chief as costume 9:
1. Ensure table[9] is populated with value 9 (or appropriate ID)
2. Ensure availability table allows slot 9
3. Modify SMI menu to include slot 9
4. Provide R_HERO09.WAD file
""")
