# Costume System Investigation - Final Report

**Date:** 2026-01-21
**Repository:** SpartanPort-Extender
**Branch:** main | **Commit:** 58d3328 (wad2) | **Working tree:** Clean

---

## Executive Summary

The costume loading system uses a multi-table lookup with runtime-populated
availability data. The previously reported addresses were **incorrect**.
The actual costume code is at **0x10017b**.

**Key Finding:** Bounds checking allows indices up to 99 (0x63), suggesting
the system theoretically supports up to 100 costume slots, far exceeding the
9 costumes (0-8) that ship with the game.

---

## 1. Corrected Addresses

| Component | Previously Reported | Actual | Location |
|---|---|---|---|
| Costume code | 0x101bab | **0x10017b** | Code segment |
| Selection variable | 0x3c2b5c | **0x3c112c** | Data (file-backed) |
| Costume mapping table | 0x420564 | **0x41eb34** | BSS |
| Availability table | (unknown) | **0x41e810** | BSS |
| R_Hero%02d string | (unknown) | **0x34f8a0** | Code segment |

---

## 2. Executable Identity

- **File:** C:\Games\gow3-windows\Rom\eboot.bin
- **Size:** 4,580,891 bytes
- **SHA-256:** 6E72330E...95B3C (matches user-provided hash)
- **Format:** PS4 SELF with embedded ELF at offset 0x120

### ELF Segments

| Segment | VAddr | File Off | FileSz | MemSz | Flags |
|---|---|---|---|---|---|
| Code | 0x0 | 0x4000 | 0x3bd43c | 0x3bd43c | R+X |
| Data | 0x3c0000 | 0x3c4000 | 0x498e8 | 0x17874f8 | R+W |

BSS starts at 0x4098e8. Contains 1498 unique referenced addresses.

---

## 3. Costume Loading Logic (0x10017b)

```
0x10017b: lea rax, [rip+0x2c0faa]   ; rax = &selection (0x3c112c)
0x100182: mov ecx, [rax]            ; ecx = selection value
0x100184: mov eax, ecx              ; eax = selection
0x100186: lea rcx, [rip+0x31e9a7]   ; rcx = &mapping_table (0x41eb34)
0x10018d: mov ecx, [rcx+rax*4]      ; ecx = mapping_table[selection]
0x100190: mov [rip+0x2c0faa], ecx   ; selection = mapped value
0x100196: lea rdi, [rip+0x35f8c3]   ; rdi = format string
0x10019d: lea rdx, [rip+0x24f6fc]   ; rdx = "R_Hero%02d"
0x1001a4: mov esi, 0x14             ; buffer size = 20
0x1001ab: call 0x2e85f0             ; sprintf-like function
```

**Logic:**
1. Read selection from 0x3c112c
2. Index into mapping table at 0x41eb34 to get archive ID
3. Store mapped ID back to selection variable
4. Format as "R_Hero%02d" with archive ID
5. Load resulting WAD file

---

## 4. Bounds Checking (0x1d55e)

```
0x01d55e: cmp r13d, 0x63            ; Compare with 99
0x01d562: ja  0x1d818               ; Skip if > 99
0x01d56b: lea rcx, [rip+0x40129e]   ; rcx = &availability (0x41e810)
0x01d572: cmp dword [rcx+rax*4], 0  ; Check if already set
0x01d57c: mov dword [rcx+rax*4], 1  ; Set availability = 1
```

**Finding:** Indices 0-99 are allowed. Table has at least 100 entries.

---

## 5. Selection Variable References (0x3c112c)

11 references found:
- 0xff903: **Initialization** (sets to 0)
- 0x1000a0: Read for resource cleanup
- 0x1000cb: Read for iteration
- 0x100128: Read for resource access
- 0x10017b: **Costume code** (read + table lookup + write back)
- 0x10032f: Write (store pointer)
- 0x100604: Read for iteration
- 0x1007c0: Read for access
- 0x1008ed: Read for access
- 0x1009d4: Read for access

---

## 6. Table References (0x41eb34)

16 references found in code at:
0x1d506, 0x1eedb, 0x25aef, 0x25c56, 0x4f410, 0x68d9b, 0x68e9d,
0x68fed, 0x6eb39, 0xb1528, and 6 more.

---

## 7. Hypotheses

| ID | Hypothesis | Confidence |
|---|---|---|
| H1 | Tables populated at runtime (init or save load) | High |
| H2 | Engine supports up to 100 costumes (bounds check) | High |
| H3 | Adding slot 9 is feasible with table + SMI patches | Medium |
| H4 | mapping_table[9] = 9 produces R_HERO09.WAD | Medium |

---

## 8. Remaining Blockers

1. **Table population mechanism** -- How/where are 0x41eb34 and 0x41e810 filled?
2. **SMI_Shell bytecode format** -- Not fully understood (differs from SMI_R_UI)
3. **Mapping table size** -- How many entries does it actually have?
4. **Selection variable at 0x3c2b70** -- Previously mentioned but not verified

---

## 9. Recommended Implementation Plan

### Phase 1: Table Population Analysis (1-2 days)
- Search for writes to 0x41eb34 and 0x41e810
- Trace initialization code
- Document table size and population logic

### Phase 2: SMI Menu Patch (3-5 days)
- Reverse engineer SMI_Shell bytecode
- Add costume slot 9 to menu state machine
- Create Python patcher (like pause_menu.py)

### Phase 3: Executable Patch (2-3 days)
- Ensure mapping_table[9] = 9
- Ensure availability_table[9] = 1
- Remove any hidden bounds checks

### Phase 4: Mod Package (1 day)
- R_HERO09.WAD (Master Chief)
- Patched SHELL.WAD
- Executable patches
- mod.json

### Verification
- Test on CUSA01623 and CUSA01715
- Check save compatibility
- Verify no corruption of costumes 0-8

---

## 10. Files Created

All in `out/analysis/` (outside tracked source):
- parse_self_elf.py, simple_disasm.py, find_rhero_refs.py
- costume_analysis.py, analyze_refs.py, find_all_tables.py
- entries.txt, report.txt, INVESTIGATION_REPORT.md

### Changed: None
Working tree remains clean. No source files modified.

---

## 11. Conclusion

**Feasibility:** Likely feasible (70% confidence)
**Estimated effort:** 6-10 days for working prototype
**Critical path:** Table population analysis -> SMI reverse engineering -> patches

**Next action:** Phase 1 (table population analysis) requires Ghidra or
continued Capstone-based analysis to trace writes to 0x41eb34 and 0x41e810.
