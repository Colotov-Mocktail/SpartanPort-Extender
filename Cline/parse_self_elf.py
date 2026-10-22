"""Parse SELF/ELF structure and extract segments."""
import struct
from pathlib import Path

SELF_PATH = Path(r'C:\Games\gow3-windows\Rom\eboot.bin')
data = SELF_PATH.read_bytes()

print(f"File size: {len(data)} bytes")
print(f"SHA-256: {SELF_PATH.name}")

# SELF header is at offset 0
# Check SELF magic
self_magic = struct.unpack('<I', data[0:4])[0]
print(f"\nSELF magic: 0x{self_magic:08x}")

# SELF header structure (simplified)
# The ELF starts at offset 0x120 according to the user
elf_offset = 0x120
print(f"\nELF offset in SELF: 0x{elf_offset:x}")

# Parse ELF header
elf_magic = data[elf_offset:elf_offset+4]
print(f"ELF magic: {elf_magic.hex()} (should be 7f454c46)")

if elf_magic != b'\x7fELF':
    print("ERROR: Invalid ELF magic")
    exit(1)

# ELF64 header
ei_class = data[elf_offset + 4]
ei_data = data[elf_offset + 5]
print(f"ELF class: {ei_class} (2=64-bit)")
print(f"ELF data: {ei_data} (1=little-endian, 2=big-endian)")

if ei_class != 2 or ei_data != 1:
    print("ERROR: Expected 64-bit little-endian ELF")
    exit(1)

# Parse ELF header fields
e_type = struct.unpack('<H', data[elf_offset+16:elf_offset+18])[0]
e_machine = struct.unpack('<H', data[elf_offset+18:elf_offset+20])[0]
e_entry = struct.unpack('<Q', data[elf_offset+24:elf_offset+32])[0]
e_phoff = struct.unpack('<Q', data[elf_offset+32:elf_offset+40])[0]
e_phentsize = struct.unpack('<H', data[elf_offset+54:elf_offset+56])[0]
e_phnum = struct.unpack('<H', data[elf_offset+56:elf_offset+58])[0]

print(f"\nELF type: {e_type} (2=executable)")
print(f"ELF machine: {e_machine} (0x3e=x86-64)")
print(f"ELF entry: 0x{e_entry:x}")
print(f"ELF phoff: 0x{e_phoff:x} (relative to ELF start)")
print(f"ELF phentsize: {e_phentsize}")
print(f"ELF phnum: {e_phnum}")

# Parse program headers
phdr_offset = elf_offset + e_phoff
print(f"\nProgram headers at file offset: 0x{phdr_offset:x}")

segments = []
for i in range(e_phnum):
    ph_offset = phdr_offset + i * e_phentsize
    p_type = struct.unpack('<I', data[ph_offset:ph_offset+4])[0]
    p_flags = struct.unpack('<I', data[ph_offset+4:ph_offset+8])[0]
    p_offset = struct.unpack('<Q', data[ph_offset+8:ph_offset+16])[0]
    p_vaddr = struct.unpack('<Q', data[ph_offset+16:ph_offset+24])[0]
    p_paddr = struct.unpack('<Q', data[ph_offset+24:ph_offset+32])[0]
    p_filesz = struct.unpack('<Q', data[ph_offset+32:ph_offset+40])[0]
    p_memsz = struct.unpack('<Q', data[ph_offset+40:ph_offset+48])[0]
    p_align = struct.unpack('<Q', data[ph_offset+48:ph_offset+56])[0]
    
    if p_type == 1:  # PT_LOAD
        segments.append({
            'idx': i,
            'flags': p_flags,
            'offset': p_offset,
            'vaddr': p_vaddr,
            'filesz': p_filesz,
            'memsz': p_memsz,
            'align': p_align
        })
        print(f"\nSegment {i}:")
        print(f"  Type: PT_LOAD")
        print(f"  Flags: 0x{p_flags:x} (R={'Y' if p_flags & 4 else 'N'} W={'Y' if p_flags & 2 else 'N'} X={'Y' if p_flags & 1 else 'N'})")
        print(f"  File offset: 0x{p_offset:x}")
        print(f"  Virtual addr: 0x{p_vaddr:x}")
        print(f"  File size: 0x{p_filesz:x} ({p_filesz} bytes)")
        print(f"  Memory size: 0x{p_memsz:x} ({p_memsz} bytes)")
        print(f"  Alignment: 0x{p_align:x}")
        
        # Check if our target addresses fall in this segment
        for addr in [0x101bab, 0x420564, 0x3c2b5c, 0x3c2b70]:
            if p_vaddr <= addr < p_vaddr + p_memsz:
                in_file = "YES" if addr < p_vaddr + p_filesz else "NO (BSS)"
                print(f"  Contains 0x{addr:x}: {in_file}")

print("\n" + "="*60)
print("Segment summary:")
for seg in segments:
    print(f"  Seg {seg['idx']}: vaddr=0x{seg['vaddr']:x} filesz=0x{seg['filesz']:x} memsz=0x{seg['memsz']:x} flags=0x{seg['flags']:x}")
