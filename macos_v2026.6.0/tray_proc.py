#!/usr/bin/env python3
"""
IQspeakr Tray-Subprocess.

Laeuft als eigener Python-Prozess, rein auf pyobjc. Kommt ohne Qt aus —
Qts QSystemTrayIcon auf macOS 26 rendert das Menubar-Icon nicht mehr
zuverlaessig, Qt-hostetes NSStatusItem auch nicht. Subprocess mit eigener
NSApplication-Instanz umgeht das vollstaendig.

Protokoll ueber stdin/stdout, JSON-Lines:

Parent -> child (stdin):
  {"cmd": "title", "value": "🎤"}        Icon-Text wechseln
  {"cmd": "tooltip", "value": "…"}      Tooltip setzen
  {"cmd": "menu", "items": [...]}       Menu neu aufbauen
      items: [{"sep": true}]
             {"title": "Label", "id": "action_x"}
             {"title": "Disabled", "enabled": false}
             {"title": "X", "id": "..", "checked": true}
  {"cmd": "quit"}                       Tray beenden

Child -> parent (stdout):
  {"click": "action_x"}                 User hat Menu-Eintrag geklickt
  {"ready": true}                       Tray ist sichtbar
"""

import sys
import json
import threading

from AppKit import (
    NSStatusBar, NSApplication, NSApplicationActivationPolicyAccessory,
    NSMenu, NSMenuItem,
)
from PyObjCTools import AppHelper
from Foundation import NSObject
import objc


_app = NSApplication.sharedApplication()
_app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)

_bar = NSStatusBar.systemStatusBar()
_item = _bar.statusItemWithLength_(28.0)
_item.retain()
_item.setVisible_(True)
_btn = _item.button()
if _btn is not None:
    _btn.setTitle_("🎤")
    _btn.setToolTip_("IQspeakr")


def _emit(obj):
    try:
        sys.stdout.write(json.dumps(obj) + "\n")
        sys.stdout.flush()
    except Exception:
        pass


class _Target(NSObject):
    def initWithActionId_(self, action_id):
        self = objc.super(_Target, self).init()
        if self is None:
            return None
        self._action_id = action_id
        return self

    def clicked_(self, sender):
        _emit({"click": self._action_id})
    clicked_ = objc.selector(clicked_, signature=b"v@:@")


_targets = []  # strong refs, sonst released


def _rebuild_menu(items):
    global _targets
    _targets = []
    menu = NSMenu.alloc().init()
    menu.setAutoenablesItems_(False)
    for entry in items:
        _add_menu_entry(menu, entry)
    _item.setMenu_(menu)


def _add_menu_entry(menu, entry):
    if entry.get("sep"):
        menu.addItem_(NSMenuItem.separatorItem())
        return
    title = entry.get("title", "")
    aid = entry.get("id")
    if "submenu" in entry:
        sub = NSMenu.alloc().init()
        sub.setAutoenablesItems_(False)
        for sub_entry in entry["submenu"]:
            _add_menu_entry(sub, sub_entry)
        mi = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, None, "")
        mi.setSubmenu_(sub)
        menu.addItem_(mi)
        return
    if aid is None:
        mi = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, None, "")
        mi.setEnabled_(False)
    else:
        tgt = _Target.alloc().initWithActionId_(aid)
        _targets.append(tgt)
        mi = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, "clicked:", "")
        mi.setTarget_(tgt)
        mi.setEnabled_(bool(entry.get("enabled", True)))
        if entry.get("checked"):
            mi.setState_(1)
    menu.addItem_(mi)


def _handle_cmd(cmd):
    kind = cmd.get("cmd")
    if kind == "title":
        val = cmd.get("value", "🎤")
        AppHelper.callAfter(lambda: _btn.setTitle_(val) if _btn else None)
    elif kind == "tooltip":
        val = cmd.get("value", "")
        AppHelper.callAfter(lambda: _btn.setToolTip_(val) if _btn else None)
    elif kind == "menu":
        items = cmd.get("items", [])
        AppHelper.callAfter(lambda: _rebuild_menu(items))
    elif kind == "quit":
        AppHelper.callAfter(AppHelper.stopEventLoop)


def _stdin_loop():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            cmd = json.loads(line)
        except Exception:
            continue
        _handle_cmd(cmd)
    # Parent hat stdin geschlossen — Tray beenden.
    AppHelper.callAfter(AppHelper.stopEventLoop)


threading.Thread(target=_stdin_loop, daemon=True).start()
_emit({"ready": True})
AppHelper.runEventLoop()
