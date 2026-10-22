"""Analyze references to selection variable and costume table."""
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

def disassemble(vaddr, size=128):
    """Disassemble code at virtual address."""
    code = read_bytes(vaddr, size)
    md = Cs(CS_ARCH_X86, CS_MODE_64)
    result = []
    for insn in md.disasm(code, vaddr):
        result.append((insn.address, insn.mnemonic, insn.op_str, insn.bytes))
    return result

SELECTION_ADDR = 0x3c112c
TABLE_ADDR = 0x41eb34

print("="*70)
print("ANALYZING REFERENCES TO SELECTION VARIABLE (0x3c112c)")
print("="*70)

selection_refs = [0xff903, 0x1000a0, 0x1000cb, 0x100128, 0x10017b, 
                  0x10032f, 0x100604, 0x1007c0, 0x1008ed, 0x1009d4]

for ref in selection_refs:
    print(f"\n--- Reference at 0x{ref:x} ---")
    insns = disassemble(ref, 64)
    for addr, mnem, op_str, bytes_hex in insns[:8]:
        hex_str = ' '.join(f'{b:02x}' for b in bytes_hex)
        marker = " <-- SELECTION" if addr == ref else ""
        print(f"0x{addr:06x}: {hex_str:<30s} {mnem:8s} {op_str}{marker}")

print("\n" + "="*70)
print("ANALYZING REFERENCES TO COSTUME TABLE (0x41eb34)")
print("="*70)

table_refs = [0x1d506, 0x1eedb, 0x25aef, 0x25c56, 0x4f410, 
              0x68d9b, 0x68e9d, 0x68fed, 0x6eb39, 0xb1528]

for ref in table_refs[:5]:  # Show first 5
    print(f"\n--- Reference at 0x{ref:x} ---")
    insns = disassemble(ref, 64)
    for addr, mnem, op_str, bytes_hex in insns[:8]:
        hex_str = ' '.join(f'{b:02x}' for b in bytes_hex)
        marker = " <-- TABLE" if addr == ref else ""
        print(f"0x{addr:06x}: {hex_str:<30s} {mnem:8s} {op_str}{marker}")

print("\n" + "="*70)
print("CHECKING COSTUME TABLE CONTENTS")
print("="*70)

print(f"\nTable address: 0x{TABLE_ADDR:x}")
print(f"File offset: {vaddr_to_file(TABLE_ADDR)}")

if vaddr_to_file(TABLE_ADDR) is None:
    print("Location: BSS (zero-initialized)")
    print("\nThe table is in BSS, so it's initialized to zero at runtime.")
    print("This means the game must populate it during initialization.")
    print("\nSearching for code that writes to the table...")
    
    # Look for mov instructions that write to addresses in the table range
    # Table is likely 9 entries (costumes 0-8), so 36 bytes
    table_end = TABLE_ADDR + 36
    
    code_start = 0x4000
    code_end = 0x4000 + 0x3bd43c
    
    print("\nSearching for writes to table range (0x{:x} - 0x{:x})...".format(TABLE_ADDR, table_end))
    
    # This is a simplified search; a full analysis would require more sophisticated pattern matching
    # For now, let's just look for immediate values that could be costume IDs (0-8)
    # in the vicinity of table references
    
    print("\nAnalyzing context around first table reference (0x1d506)...")
    insns = disassemble(0x1d506 - 64, 256)
    for addr, mnem, op_str, bytes_hex in insns[:40]:
        hex_str = ' '.join(f'{b:02x}' for b in bytes_hex)
        print(f"0x{addr:06x}: {hex_str:<30s} {mnem:8s} {op_str}")

print("\n" + "="*70)
print("SUMMARY")
print("="*70)

print("""
Key findings:
1. Selection variable at 0x3c112c (file-backed, initial value 0)
2. Costume table at 0x41eb34 (BSS, zero-initialized)
3. R_Hero%02d string at 0x34f8a0
4. Costume code at 0x10017b loads selection, indexes table, formats string

The costume table is in BSS, which means it's populated at runtime.
The game likely initializes it with costume availability or mapping data
during startup or when loading save data.

To add a 9th costume slot, we would need to:
1. Find where the table is populated
2. Extend the table to include slot 9
3. Modify any bounds checking on the selection variable
4. Ensure the SMI menu can select slot 9
""")
