"""Stage the minimal set of Tesseract files to bundle into Screen2PDF.exe.

Copies tesseract.exe plus only the DLLs it actually loads (found by walking
PE import tables), skipping the ~200 MB of training tools in the install,
and strips their unused debug sections.

Usage: python bundle_tesseract.py [tesseract_install_dir] [output_dir]
"""

import os
import shutil
import sys

import pefile

DEFAULT_INSTALL = r"C:\Program Files\Tesseract-OCR"
DEFAULT_OUT = os.path.join("build", "tesseract_bundle")


def needed_dlls(install_dir: str, exe: str) -> set[str]:
    """DLL names (lowercase) from install_dir that exe needs, followed recursively."""
    available = {f.lower(): f for f in os.listdir(install_dir) if f.lower().endswith(".dll")}
    needed, todo = set(), [exe]
    while todo:
        pe = pefile.PE(os.path.join(install_dir, todo.pop()), fast_load=True)
        pe.parse_data_directories(directories=[pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_IMPORT"]])
        for entry in getattr(pe, "DIRECTORY_ENTRY_IMPORT", []):
            name = entry.dll.decode().lower()
            if name in available and name not in needed:  # system DLLs aren't in the install
                needed.add(name)
                todo.append(available[name])
        pe.close()
    return {available[n] for n in needed}


def strip_debug_sections(path: str) -> None:
    """Drop trailing DWARF debug sections that MinGW builds leave in the binary.

    They are never loaded at runtime but make up ~90% of libtesseract's size,
    which would make the one-file exe slow to unpack on every launch.
    """
    pe = pefile.PE(path)
    secs = pe.sections
    keep = len(secs)
    while keep and secs[keep - 1].Name.rstrip(b"\0").startswith((b"/", b".debug")):
        keep -= 1
    if keep == len(secs) or keep == 0:
        pe.close()
        return
    cut = min(s.PointerToRawData for s in secs[keep:])
    # The packager's Authenticode signature sits at the end of the file and can't
    # survive stripping anyway, so it is dropped too (Windows loads unsigned DLLs fine).
    cert = pe.OPTIONAL_HEADER.DATA_DIRECTORY[pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_SECURITY"]]
    if (cert.Size and cert.VirtualAddress < cut) or any(
        s.PointerToRawData + s.SizeOfRawData > cut for s in secs[:keep]
    ):
        pe.close()
        return  # unusual layout; leave the file alone
    cert.VirtualAddress = cert.Size = 0
    last = secs[keep - 1]
    align = pe.OPTIONAL_HEADER.SectionAlignment
    pe.FILE_HEADER.NumberOfSections = keep
    pe.FILE_HEADER.PointerToSymbolTable = 0
    pe.FILE_HEADER.NumberOfSymbols = 0
    pe.OPTIONAL_HEADER.SizeOfImage = -(-(last.VirtualAddress + last.Misc_VirtualSize) // align) * align
    data = bytearray(pe.write()[:cut])
    for s in secs[keep:]:  # blank the now-unused section headers
        off = s.get_file_offset()
        data[off:off + s.sizeof()] = bytes(s.sizeof())
    pe.close()
    with open(path, "wb") as f:
        f.write(data)


def main() -> None:
    install = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_INSTALL
    out = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_OUT
    if not os.path.isfile(os.path.join(install, "tesseract.exe")):
        sys.exit(f"tesseract.exe not found in {install}")

    shutil.rmtree(out, ignore_errors=True)
    os.makedirs(out)
    files = ["tesseract.exe", *sorted(needed_dlls(install, "tesseract.exe"))]
    for f in files:
        shutil.copy2(os.path.join(install, f), out)
        strip_debug_sections(os.path.join(out, f))
    size = sum(os.path.getsize(os.path.join(out, f)) for f in files)
    print(f"Staged {len(files)} files ({size / 2**20:.0f} MB) in {out}")


if __name__ == "__main__":
    main()
