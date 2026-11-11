"""Build a single-file, self-contained ut325f GUI for distribution.

    python build_standalone.py

Reads ut325f_protocol.py + ut325f_gui.py and writes ut325f_gui_standalone.py
with the protocol module inlined in place of its import. The split files stay
the editable source; the standalone is generated, so the two cannot diverge.
"""
import pathlib
import re
import sys

PROTOCOL = pathlib.Path("ut325f_protocol.py")
GUI = pathlib.Path("ut325f_gui.py")
OUT = pathlib.Path("ut325f_gui_standalone.py")
IMPORT_LINE = re.compile(r"^from ut325f_protocol import .*$", re.M)

BANNER = """# ---------------------------------------------------------------------------
# GENERATED FILE - do not edit.
# Built by build_standalone.py from ut325f_gui.py + ut325f_protocol.py.
# Edit those and re-run the build; changes made here will be overwritten.
# ---------------------------------------------------------------------------
"""


def inlined_protocol():
    src = PROTOCOL.read_text(encoding="utf-8")
    # turn the module docstring into comments so it does not become a stray
    # expression statement in the middle of the merged file
    if src.lstrip().startswith('"""'):
        start = src.index('"""')
        end = src.index('"""', start + 3) + 3
        doc = src[start + 3:end - 3].strip("\n")
        body = src[end:]
        doc = "\n".join(("# " + ln).rstrip() for ln in doc.splitlines())
    else:
        doc, body = "", src
    return ("# ===== begin inlined ut325f_protocol.py =====\n"
            + doc + "\n" + body.strip("\n")
            + "\n# ===== end inlined ut325f_protocol.py =====\n")


def main():
    for f in (PROTOCOL, GUI):
        if not f.exists():
            print(f"missing {f}")
            return 1
    gui = GUI.read_text(encoding="utf-8")
    if not IMPORT_LINE.search(gui):
        print("could not find the 'from ut325f_protocol import ...' line in", GUI)
        return 1
    block = inlined_protocol().rstrip("\n")
    # lambda replacement: re.sub would otherwise interpret the \xAA
    # escapes in the protocol source as replacement escapes
    merged = BANNER + IMPORT_LINE.sub(lambda m: block, gui, count=1)
    OUT.write_text(merged, encoding="utf-8")
    print(f"wrote {OUT} ({len(merged.splitlines())} lines)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
