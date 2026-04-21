#!/usr/bin/env python3
"""
Debug-Tool für spreakr: Zeigt alle Tastendrücke + Keycodes.
Start:  python3 key_debug.py
Stop:   Ctrl+C im Terminal
"""
import sys
from AppKit import NSEvent, NSApplication, NSApp
from PyObjCTools import AppHelper

KNOWN = {
    55: "cmd (⌘)",
    56: "shift (⇧)",
    58: "alt/option (⌥)",
    59: "ctrl (⌃) LINKS",
    62: "ctrl (⌃) RECHTS",
    54: "cmd RECHTS",
    60: "shift RECHTS",
    61: "alt RECHTS",
    63: "fn",
    49: "space",
    36: "return",
    51: "backspace",
    53: "escape",
    48: "tab",
}

FLAG_BITS = {
    1 << 16: "CAPSLOCK",
    1 << 17: "SHIFT",
    1 << 18: "CTRL",
    1 << 19: "ALT",
    1 << 20: "CMD",
    1 << 23: "FN",
}

def decode_flags(flags):
    active = [name for bit, name in FLAG_BITS.items() if flags & bit]
    return "+".join(active) if active else "—"

def handler(event):
    etype = event.type()
    kc = event.keyCode()
    flags = event.modifierFlags()
    name = KNOWN.get(kc, f"(unbekannt)")
    if etype == 12:  # FlagsChanged
        print(f"MODIFIER  keycode={kc:3d}  {name:20s}  flags=0x{flags:x}  aktiv={decode_flags(flags)}")
    elif etype == 10:  # KeyDown
        try:
            chars = event.charactersIgnoringModifiers()
        except Exception:
            chars = "?"
        print(f"KEYDOWN   keycode={kc:3d}  {name:20s}  zeichen='{chars}'  flags=0x{flags:x}  aktiv={decode_flags(flags)}")
    sys.stdout.flush()

def main():
    print("=" * 70)
    print("spreakr Key-Debug — drücke Tasten auf deiner Bluetooth-Tastatur.")
    print("Beende mit Ctrl+C im Terminal.")
    print("=" * 70)
    sys.stdout.flush()

    NSApplication.sharedApplication()
    mask = (1 << 10) | (1 << 12)  # NSKeyDown + NSFlagsChanged
    monitor = NSEvent.addGlobalMonitorForEventsMatchingMask_handler_(mask, handler)
    if monitor is None:
        print("FEHLER: NSEvent-Monitor konnte nicht erstellt werden.")
        print("→ Bedienungshilfen-Berechtigung für Terminal/Python fehlt.")
        sys.exit(1)
    AppHelper.runEventLoop()

if __name__ == "__main__":
    main()
