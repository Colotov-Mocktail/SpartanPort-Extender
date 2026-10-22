"""Simple disassembly dump around 0x101bab."""
import struct
from pathlib import Path
from capstone import Cs, CS_ARCH_X86, CS_MODE_64

SELF_PATH = Path(r'C:\Games\gow3-windows\Rom\eboot.bin')
data = SELF_PATH.read_bytes()

# Code segment: vaddr=0x0, file_offset=0x4000
# To read vaddr X, read from file offset 0x4000 + X

def vaddr_to_file(vaddr):
    """Convert virtual address to file offset."""
    # Code segment: vaddr 0x0 to 0x3bd43c
    if 0x0 <= vaddr < 0x3bd43c:
        return 0x4000 + vaddr
    # Data segment: vaddr 0x3c0000 to 0x3c0000+0x498e8
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
print("DISASSEMBLY AT 0x101bab")
print("="*70)

insns = disassemble(0x101bab, 128)
for addr, mnem, op_str, bytes_hex in insns[:20]:
    hex_str = ' '.join(f'{b:02x}' for b in bytes_hex)
    print(f"0x{addr:06x}: {hex_str:<30s} {mnem:8s} {op_str}")

print("\n" + "="*70)
print("RAW BYTES AT 0x101bab")
print("="*70)

raw = read_bytes(0x101bab, 64)
for i in range(0, len(raw), 16):
    hex_str = ' '.join(f'{b:02x}' for b in raw[i:i+16])
    print(f"0x{0x101bab+i:06x}: {hex_str}")

print("\n" + "="*70)
print("CHECK STRING AT 0x3512d0")
print("="*70)

string_bytes = read_bytes(0x3512d0, 64)
print(f"Raw bytes: {string_bytes[:32].hex()}")
string_str = string_bytes.split(b'\x00')[0].decode('ascii', errors='replace')
print(f"String: '{string_str}'")

print("\n" + "="*70)
print("SEARCH FOR 'R_Hero' STRING")
print("="*70)

# Search for R_Hero in the entire file
search_term = b'R_Hero'
pos = 0
found = []
while True:
    pos = data.find(search_term, pos)
    if pos == -1:
        break
    # Check if this is in a code or data segment
    # Code segment: file offset 0x4000 to 0x4000+0x3bd43c
    # Data segment: file offset 0x3c4000 to 0x3c4000+0x498e8
    if 0x4000 <= pos < 0x4000 + 0x3bd43c:
        vaddr = pos - 0x4000
        seg = "code"
    elif 0x3c4000 <= pos < 0x3c4000 + 0x498e8:
        vaddr = 0x3c0000 + (pos - 0x3c4000)
        seg = "data"
    else:
        vaddr = None
        seg = "other"
    
    ctx = data[pos:pos+32]
    ctx_str = ctx.split(b'\x00')[0].decode('ascii', errors='replace')
    found.append((pos, vaddr, seg, ctx_str))
    pos += 1

print(f"Found {len(found)} occurrences of 'R_Hero':")
for file_off, vaddr, seg, ctx in found[:10]:
    vaddr_str = f"vaddr=0x{vaddr:x}" if vaddr is not None else "vaddr=N/A"
    print(f"  file_off=0x{file_off:x} {vaddr_str} seg={seg}: '{ctx}'")
