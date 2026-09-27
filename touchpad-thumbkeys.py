#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""touchpad-thumbkeys daemon.

Intercepts low-level evdev touchpad events, splits the top edge into two
thumb zones, emits uinput keycodes when a touch originates inside a zone, and
routes standard mouse tracking events to a virtual touchpad.
"""

from __future__ import annotations

import atexit
import fcntl
import os
import select
import signal
import struct
import subprocess
import sys
import threading
import time

# =============================================================================
# Configuration
# =============================================================================

# Hardcoded device name from /proc/bus/input/devices (e.g., "SYNA32B8:00 06CB:CE17 Touchpad").
# Set to None to auto-detect. Also prompts if in interactive shell and no device was found.
TOUCHPAD_DEVICE_NAME: str | None = None

# Vertical height of the top thumb zone (in raw device units from top edge).
# Set a little bit above your largest reported Y value.
ZONE_HEIGHT: int = 300

# Center X division line in raw device units.
# Set to None for exact center split, or set to midpoint between your two thumb's X values.
CENTER_X: int | None = 1100

# Action to perform when a thumb zone is triggered.
# Can be:
#  - int: A single Linux EV_KEY kernel integer (e.g., `94`)
#       - Full list at # https://github.com/torvalds/linux/blob/master/include/uapi/linux/input-event-codes.h
#  - list[int]: A combination of keycodes pressed simultaneously (e.g., `[29, 46]` for Ctrl+C)
#  - str: A shell command executed asynchronously (e.g., "notify-send 'Thumbkey Pressed'") 
#    (!! DANGER !!)   This command will run with full root privileges.   (!! DANGER !!)
#
# Modifier Keycodes: 29=L-Ctrl, 42= L-Shift, 125=L-Meta, 56=L-Alt, 100=R-Alt (AltGr)
# Keycodes for clean Kanata consumption: 94=Muhenkan, 92=Henkan (JIS left and right thumb keys)

ACTION_TOP_LEFT: int | list[int] | str = 94
ACTION_TOP_RIGHT: int | list[int] | str = 92

# Hold Time (in milliseconds): The finger must remain in the thumb zone for this long
# before the action is triggered. Set to 0 for instant activation.
HOLD_TIME_TOP_LEFT: int = 0
HOLD_TIME_TOP_RIGHT: int = 0

# Slide-Out Behavior: If True, dragging a finger out of the thumb zone releases 
# the synthetic keypress and transitions to regular touchpad pointer movement.
SLIDE_OUT_TOP_LEFT: bool = True
SLIDE_OUT_TOP_RIGHT: bool = True

# Two-finger touches that land in the same zone always switch to touchpad output (for scrolling),
# but two-finger touches that land in *separate* zones are interpreted as two thumb keys.
# Exclusive Thumb Mode: If True, landing a finger in that different thumb zone while one 
# is already active cancels both keypresses and transitions all fingers to touchpad mode.
# Reduces false positives, but only enable if you do not need to activate your thumb keys simultaneously.
EXCLUSIVE_THUMB_KEYS: bool = False

# KDE Settings Sync: If True, queries KDE Plasma's D-Bus interface to clone
# the captured touchpad's settings onto the virtual pointer device upon startup.
# Gnome and WLRoots-based desktops use global touchpad settings.
KDE_INHERIT_TOUCHPAD_SETTINGS: bool = True

# =============================================================================
# Linux Kernel Input Protocol Constants
# =============================================================================

EV_SYN: int = 0
EV_KEY: int = 1
EV_ABS: int = 3

SYN_REPORT: int = 0

ABS_X: int = 0x00
ABS_Y: int = 0x01
ABS_MT_SLOT: int = 0x2F
ABS_MT_POSITION_X: int = 0x35
ABS_MT_POSITION_Y: int = 0x36
ABS_MT_TRACKING_ID: int = 0x39

BTN_LEFT: int = 0x110
BTN_TOUCH: int = 0x14A
BTN_TOOL: list[int] = [0x145, 0x14D, 0x14E, 0x14F, 0x148]  # Finger count tools

INPUT_PROP_POINTER: int = 0
INPUT_PROP_BUTTONPAD: int = 2

# Linux ioctl codes
EVIOCGRAB: int = 0x40044590
UI_DEV_CREATE: int = 0x5501
UI_DEV_DESTROY: int = 0x5502
UI_DEV_SETUP: int = 0x405C5503
UI_ABS_SETUP: int = 0x401C5504
UI_SET_EVBIT: int = 0x40045564
UI_SET_KEYBIT: int = 0x40045565
UI_SET_ABSBIT: int = 0x40045567
UI_SET_PROPBIT: int = 0x4004556E

EVENT_STRUCT: struct.Struct = struct.Struct("llHHi")

DEBUG: bool = "--debug" in sys.argv


def log(*args: object) -> None:
    """Print detailed trace output only if --debug flag is passed."""
    if DEBUG:
        print(*args, flush=True)


def EVIOCGABS(axis: int) -> int:
    """Calculate the ioctl code for reading absolute axis information."""
    return 0x80184540 + axis


# =============================================================================
# Device Discovery
# =============================================================================

def find_event_node(target_name: str | None = None) -> tuple[str, str]:
    """Locate the touchpad input node.

    Discovery Priority:
    1. Reconnect or Hardcoded Name (strictly requires exact match; no fallback).
    2. Auto-detection matching 'touchpad' in /proc/bus/input/devices.
    3. Interactive selection menu (only if running interactively in a TTY).

    Returns a tuple of (event_node_path, device_name).
    """
    devices: list[tuple[str, str]] = []
    current_name: str = ""

    if os.path.exists("/proc/bus/input/devices"):
        for line in open("/proc/bus/input/devices"):
            if line.startswith("N:"):
                current_name = line.split('"')[1]
            elif line.startswith("H:"):
                handlers = line.split("=", 1)[1].split()
                for event_node in handlers:
                    if event_node.startswith("event"):
                        devices.append((event_node, current_name))

    search_target = target_name or TOUCHPAD_DEVICE_NAME

    if search_target:
        for event_node, name in devices:
            if name == search_target:
                print(f"[Device] Matched target '{name}' on /dev/input/{event_node}", flush=True)
                return f"/dev/input/{event_node}", name

        if target_name:
            raise FileNotFoundError(f"Target device '{target_name}' not currently available.")
        sys.exit(f"Fatal: Hardcoded device '{search_target}' not found in /proc/bus/input/devices. Aborting.")

    for event_node, name in devices:
        name_lower = name.lower()
        if "touchpad" in name_lower and "virtual" not in name_lower and "thumbkeys" not in name_lower:
            print(f"Auto-detected touchpad: '{name}' on /dev/input/{event_node}", flush=True)
            return f"/dev/input/{event_node}", name

    if not sys.stdin.isatty():
        sys.exit("Fatal: Could not auto-detect Touchpad while running non-interactively (systemd).")

    print("\nCould not automatically identify a physical Touchpad.")
    print("Available Input Devices:")
    for idx, (event_node, name) in enumerate(devices):
        print(f"  [{idx}] /dev/input/{event_node} - {name}")
    print("  [q] Quit")

    try:
        choice = input("\nSelect device number to capture (or 'q' to exit): ").strip()
        if choice.lower() == "q" or not choice.isdigit():
            sys.exit("Exiting: No device selected.")

        selected_idx = int(choice)
        if 0 <= selected_idx < len(devices):
            event_node, name = devices[selected_idx]
            node_path = f"/dev/input/{event_node}"
            print(f"Selected: {name} on {node_path}", flush=True)
            return node_path, name
        sys.exit("Exiting: Invalid selection.")
    except (KeyboardInterrupt, EOFError):
        sys.exit("\nExiting: Selection cancelled.")


# =============================================================================
# Virtual Input Interface (uinput)
# =============================================================================

class UInput:
    """Wrapper around /dev/uinput for emitting synthetic hardware events."""

    def __init__(
        self,
        name: str,
        keys: tuple[int, ...] | list[int] = (),
        abs_axes: dict[int, bytes] | None = None,
        props: tuple[int, ...] | list[int] = (),
    ) -> None:
        self.fd: int = os.open("/dev/uinput", os.O_WRONLY | os.O_NONBLOCK)

        fcntl.ioctl(self.fd, UI_SET_EVBIT, EV_KEY)
        for key in keys:
            fcntl.ioctl(self.fd, UI_SET_KEYBIT, key)

        if abs_axes:
            fcntl.ioctl(self.fd, UI_SET_EVBIT, EV_ABS)
            for axis, info in abs_axes.items():
                fcntl.ioctl(self.fd, UI_SET_ABSBIT, axis)
                fcntl.ioctl(self.fd, UI_ABS_SETUP, struct.pack("HH", axis, 0) + info)

        for prop in props:
            fcntl.ioctl(self.fd, UI_SET_PROPBIT, prop)

        setup = (
            struct.pack("HHHH", 0x06, 0x0B05, 0x0001, 1)
            + name.encode().ljust(80, b"\0")
            + struct.pack("I", 0)
        )
        fcntl.ioctl(self.fd, UI_DEV_SETUP, setup)
        fcntl.ioctl(self.fd, UI_DEV_CREATE)

    def emit(self, etype: int, code: int, value: int) -> None:
        """Write a single raw input event struct to uinput."""
        os.write(self.fd, EVENT_STRUCT.pack(0, 0, etype, code, value))

    def syn(self) -> None:
        """Emit a SYN_REPORT signal to flush pending input events."""
        self.emit(EV_SYN, SYN_REPORT, 0)

    def close(self) -> None:
        """Unregister the virtual device and close the file descriptor safely."""
        if getattr(self, "closed", False):
            return
        self.closed = True
        try:
            fcntl.ioctl(self.fd, UI_DEV_DESTROY)
        except OSError:
            pass
        finally:
            try:
                os.close(self.fd)
            except OSError:
                pass


# =============================================================================
# Touch State & Daemon Core
# =============================================================================

class Touch:
    """Tracks state and coordinate lifecycle for a single multi-touch slot."""

    def __init__(self, tid: int) -> None:
        self.tid: int = tid
        self.x: int | None = None
        self.y: int | None = None
        self.start_time: float = time.monotonic()
        self.origin_evaluated: bool = False
        self.is_thumb_zone: bool = False
        self.zone_name: str | None = None
        self.action: int | list[int] | str | None = None
        self.hold_time_ms: int = 0
        self.action_active: bool = False
        self.allow_slide_out: bool = False
        self.pointer: bool = False
        self.started: bool = False
        self.ended: bool = False


class TouchZoneDaemon:
    """Core event loop for grabbing touchpad input and converting zones to keycodes."""

    def __init__(self) -> None:
        self.src_path, self.target_name = find_event_node()
        self.src: int = os.open(self.src_path, os.O_RDONLY | os.O_NONBLOCK)

        absinfo: dict[int, bytes] = {}
        for axis in (ABS_X, ABS_Y, ABS_MT_SLOT, ABS_MT_POSITION_X, ABS_MT_POSITION_Y, ABS_MT_TRACKING_ID):
            buf = bytearray(24)
            fcntl.ioctl(self.src, EVIOCGABS(axis), buf)
            absinfo[axis] = bytes(buf)

        _, _, self.max_x, *_ = struct.unpack("6i", absinfo[ABS_X])
        _, _, self.max_y, *_ = struct.unpack("6i", absinfo[ABS_Y])

        self.center_x: int = CENTER_X if CENTER_X is not None else self.max_x // 2

        self.virtual_pointer_name: str = "Touchpad Thumbkeys Virtual Pointer"
        
        # Dynamically aggregate all keycodes defined in the config to register with uinput
        kbd_keys = set[int]()
        for action in (ACTION_TOP_LEFT, ACTION_TOP_RIGHT):
            if isinstance(action, int):
                kbd_keys.add(action)
            elif isinstance(action, (list, tuple)):
                kbd_keys.update(action)
                
        self.kbd: UInput = UInput("Touchpad Thumbkeys Keyboard", keys=list(kbd_keys))
        
        self.pad: UInput = UInput(
            self.virtual_pointer_name,
            keys=[BTN_LEFT, BTN_TOUCH] + BTN_TOOL,
            abs_axes=absinfo,
            props=[INPUT_PROP_POINTER, INPUT_PROP_BUTTONPAD],
        )

        if KDE_INHERIT_TOUCHPAD_SETTINGS:
            threading.Thread(
                target=self.sync_kde_settings,
                args=(self.src_path, self.virtual_pointer_name),
                daemon=True,
            ).start()

        self.slot: int = 0
        self.touches: dict[int, Touch] = {}
        self.last_pos: dict[int, tuple[int | None, int | None]] = {}
        self.click: int = 0

        fcntl.ioctl(self.src, EVIOCGRAB, 1)
        print(f"[Device] Acquired exclusive EVIOCGRAB on '{self.target_name}' ({self.src_path})", flush=True)

    def trigger_action(self, action: int | list[int] | str, state: int) -> None:
        """Emit uinput keys or execute shell commands based on the action type."""
        if isinstance(action, str):
            if state == 1:
                subprocess.Popen(action, shell=True)
        elif isinstance(action, int):
            self.kbd.emit(EV_KEY, action, state)
            self.kbd.syn()
        else:
            # On release, lift keys in the reverse order they were pressed
            keys = action if state == 1 else reversed(action)
            for k in keys:
                self.kbd.emit(EV_KEY, k, state)
            self.kbd.syn()

    def sync_kde_settings(self, source_path: str, virtual_name: str) -> None:
        """Background worker to query and clone KDE KWin input device configuration via D-Bus."""
        import pwd
        
        # 1. Dynamically find the logged-in KDE user
        uid = None
        try:
            ps_res = subprocess.run(["ps", "-C", "kwin_wayland,kwin_x11", "-o", "uid="], capture_output=True, text=True)
            uids = [u.strip() for u in ps_res.stdout.splitlines() if u.strip()]
            if uids:
                uid = int(uids[0])
        except Exception:
            pass

        if uid is None:
            sudo_user = os.environ.get("SUDO_USER")
            if sudo_user and sudo_user != "root":
                uid = pwd.getpwnam(sudo_user).pw_uid

        if uid is None:
            print("[KDE Sync] Warning: Could not detect logged-in KDE user. Skipping sync.", flush=True)
            return

        username = pwd.getpwuid(uid).pw_name
        env = os.environ.copy()
        env["XDG_RUNTIME_DIR"] = f"/run/user/{uid}"
        env["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path=/run/user/{uid}/bus"

        # Helper to execute busctl safely inside the user's D-Bus session
        def run_busctl(*args: str) -> subprocess.CompletedProcess[str]:
            cmd = ["sudo", "-E", "-u", username, "busctl", "--user"] + list(args)
            try:
                return subprocess.run(cmd, capture_output=True, text=True, env=env)
            except FileNotFoundError as e:
                return subprocess.CompletedProcess(args=cmd, returncode=1, stdout="", stderr=str(e))

        src_sys_name = os.path.basename(source_path)
        src_dbus_path = f"/org/kde/KWin/InputDevice/{src_sys_name}"

        print(f"[KDE Sync] Bridging D-Bus session for user '{username}' (UID: {uid})...", flush=True)
        virt_sys_name = None

        # 2. Poll KWin D-Bus tree indefinitely (waits for KWin to finish booting if at login screen)
        while True:
            time.sleep(2.0)
            res = run_busctl("tree", "org.kde.KWin")
            
            if res.returncode != 0:
                continue

            for line in res.stdout.splitlines():
                if "/org/kde/KWin/InputDevice/event" in line:
                    path = line.strip().split()[-1]
                    name_res = run_busctl("get-property", "org.kde.KWin", path, "org.kde.KWin.InputDevice", "name")
                    
                    if virtual_name.lower() in name_res.stdout.lower() or "thumbkeys" in name_res.stdout.lower():
                        virt_sys_name = path.split("/")[-1]
                        break
            if virt_sys_name:
                break

        virt_dbus_path = f"/org/kde/KWin/InputDevice/{virt_sys_name}"
        print(f"[KDE Sync] Matching source ({src_dbus_path}) -> virtual ({virt_dbus_path})", flush=True)

        properties: list[str] = [
            "enabled", "disableEventsOnExternalMouse", "leftHanded", 
            "middleButtonEmulation", "pointerAcceleration", "pointerAccelerationProfile", 
            "scrollFactor", "scrollMethod", "naturalScroll", "clickMethod", 
            "tapToClick", "tapAndDrag", "tapDragLock", "lmrTapButtonMap"
        ]

        # 3. Clone properties
        for prop in properties:
            get_res = run_busctl("get-property", "org.kde.KWin", src_dbus_path, "org.kde.KWin.InputDevice", prop)
            if get_res.returncode == 0:
                parts = get_res.stdout.strip().split(maxsplit=1)
                if len(parts) == 2:
                    sig, val = parts
                    
                    set_res = run_busctl("set-property", "org.kde.KWin", virt_dbus_path, "org.kde.KWin.InputDevice", prop, sig, val)
                    if set_res.returncode == 0:
                        log(f"[KDE Sync] Synced {prop} ({val})")
                    else:
                        log(f"[KDE Sync] Failed to set {prop}: {set_res.stderr.strip()}")
            else:
                log(f"[KDE Sync] Skipping {prop} (not exposed by source device)")

        # 4. Hardcoded override
        run_busctl("set-property", "org.kde.KWin", virt_dbus_path, "org.kde.KWin.InputDevice", "disableWhileTyping", "b", "false")
        log("[KDE Sync] Overrode disableWhileTyping (Forced to false)")

        print("[KDE Sync] Configuration cloned successfully.", flush=True)

    def zone_at(self, x: int, y: int) -> str | None:
        """Determine if X/Y coordinates land inside a thumb zone."""
        if y < ZONE_HEIGHT:
            return "top_right" if x >= self.center_x else "top_left"
        return None

    def handle_event(self, etype: int, code: int, value: int) -> None:
        """Parse incoming evdev signals from the hardware node."""
        if etype == EV_ABS:
            if code == ABS_MT_SLOT:
                self.slot = value
            elif code == ABS_MT_TRACKING_ID:
                if value >= 0:
                    t = self.touches[self.slot] = Touch(value)
                    t.x, t.y = self.last_pos.get(self.slot, (None, None))
                elif self.slot in self.touches:
                    self.touches[self.slot].ended = True
                    log(f"[Slot {self.slot}] Contact Lifted")
            elif code in (ABS_MT_POSITION_X, ABS_MT_POSITION_Y) and self.slot in self.touches:
                t = self.touches[self.slot]
                if code == ABS_MT_POSITION_X:
                    t.x = value
                else:
                    t.y = value
                self.last_pos[self.slot] = (t.x, t.y)
        elif etype == EV_KEY and code == BTN_LEFT:
            self.click = value
            log(f"Physical Click State: {value}")
        elif etype == EV_SYN and code == SYN_REPORT:
            self.process_frame()

    def get_poll_timeout(self) -> float:
        """Calculate how long to block in select() before processing held keys."""
        timeout = 1.0
        now = time.monotonic()
        for t in self.touches.values():
            if t.is_thumb_zone and not t.action_active and not t.ended and t.hold_time_ms > 0:
                time_left = (t.hold_time_ms / 1000.0) - (now - t.start_time)
                if time_left <= 0:
                    return 0.0
                if time_left < timeout:
                    timeout = time_left
        return timeout

    def process_held_keys(self) -> None:
        """Trigger actions for touches that have met their required hold time."""
        now = time.monotonic()
        for t in self.touches.values():
            if t.is_thumb_zone and not t.action_active and not t.ended:
                if (now - t.start_time) * 1000 >= t.hold_time_ms:
                    assert t.action is not None
                    self.trigger_action(t.action, 1)
                    t.action_active = True
                    
                    label = "Instant" if t.hold_time_ms == 0 else f"Held {t.hold_time_ms}ms"
                    print(f"=== ACTION DOWN ({label}): {t.action} ===", flush=True)

    def process_frame(self) -> None:
        """Process complete event frames and route to keyboard or pointer uinput."""
        for t in list(self.touches.values()):
            if not t.origin_evaluated and t.x is not None and t.y is not None:
                t.origin_evaluated = True
                zone = self.zone_at(t.x, t.y)

                if zone is not None:
                    t.is_thumb_zone = True
                    t.zone_name = zone
                    if zone == "top_right":
                        t.action = ACTION_TOP_RIGHT
                        t.allow_slide_out = SLIDE_OUT_TOP_RIGHT
                        t.hold_time_ms = HOLD_TIME_TOP_RIGHT
                    else:
                        t.action = ACTION_TOP_LEFT
                        t.allow_slide_out = SLIDE_OUT_TOP_LEFT
                        t.hold_time_ms = HOLD_TIME_TOP_LEFT
                    log(f"[Landing] X:{t.x} Y:{t.y} -> ZONE '{zone}' (Action: {t.action}, Hold: {t.hold_time_ms}ms, SlideOut: {t.allow_slide_out})")
                else:
                    t.is_thumb_zone = False
                    t.pointer = True
                    log(f"[Landing] X:{t.x} Y:{t.y} -> POINTER MODE")

        active_thumb_touches = [t for t in self.touches.values() if t.is_thumb_zone and not t.ended]
        active_zones = [t.zone_name for t in active_thumb_touches if t.zone_name is not None]
        has_same_zone_duplicate = len(active_zones) != len(set(active_zones))
        has_exclusive_conflict = EXCLUSIVE_THUMB_KEYS and len(active_thumb_touches) >= 2

        if has_same_zone_duplicate or has_exclusive_conflict:
            for t in active_thumb_touches:
                if t.action_active:
                    assert t.action is not None
                    self.trigger_action(t.action, 0)
                    t.action_active = False
                    reason = "Same-Zone Duplicate" if has_same_zone_duplicate else "Exclusive Conflict"
                    print(f"=== ACTION UP ({reason}): {t.action} -> POINTER MODE ===", flush=True)

                t.is_thumb_zone = False
                t.pointer = True

        for t in list(self.touches.values()):
            if t.is_thumb_zone and t.allow_slide_out and t.y is not None and t.y >= ZONE_HEIGHT:
                if t.action_active:
                    assert t.action is not None
                    self.trigger_action(t.action, 0)
                    t.action_active = False
                    print(f"=== ACTION UP (Zone Exit): {t.action} -> POINTER MODE ===", flush=True)
                
                t.is_thumb_zone = False
                t.pointer = True

            if t.action_active and t.ended:
                assert t.action is not None
                self.trigger_action(t.action, 0)
                t.action_active = False
                print(f"=== ACTION UP: {t.action} ===", flush=True)

        self.process_held_keys()
        self.forward_pointer_events()

        for slot in [s for s, t in self.touches.items() if t.ended]:
            del self.touches[slot]

    def forward_pointer_events(self) -> None:
        """Forward non-zone touches to the virtual uinput pointer device."""
        for s, t in self.touches.items():
            if not t.pointer or t.x is None or t.y is None:
                continue

            self.pad.emit(EV_ABS, ABS_MT_SLOT, s)
            if not t.started:
                self.pad.emit(EV_ABS, ABS_MT_TRACKING_ID, t.tid)
                t.started = True

            if t.ended:
                self.pad.emit(EV_ABS, ABS_MT_TRACKING_ID, -1)
            else:
                self.pad.emit(EV_ABS, ABS_MT_POSITION_X, t.x)
                self.pad.emit(EV_ABS, ABS_MT_POSITION_Y, t.y)

        active_pointers = [
            t for t in self.touches.values()
            if t.pointer and t.started and not t.ended and t.x is not None and t.y is not None
        ]
        self.pad.emit(EV_KEY, BTN_TOUCH, 1 if active_pointers else 0)

        for i, btn in enumerate(BTN_TOOL):
            self.pad.emit(EV_KEY, btn, 1 if len(active_pointers) == i + 1 else 0)

        if active_pointers:
            first = active_pointers[0]
            assert first.x is not None and first.y is not None
            self.pad.emit(EV_ABS, ABS_X, first.x)
            self.pad.emit(EV_ABS, ABS_Y, first.y)

        self.pad.emit(EV_KEY, BTN_LEFT, self.click)
        self.pad.syn()

    def handle_suspend(self, signum: int, frame: object) -> None:
        """Release device grab if suspended via SIGTSTP (Ctrl+Z)."""
        try:
            fcntl.ioctl(self.src, EVIOCGRAB, 0)
            print("[Device] Released EVIOCGRAB on suspend (Ctrl+Z)", flush=True)
        except OSError:
            pass
        signal.signal(signal.SIGTSTP, signal.SIG_DFL)
        os.kill(os.getpid(), signal.SIGTSTP)

    def handle_resume(self, signum: int, frame: object) -> None:
        """Re-acquire device grab when foregrounded via SIGCONT (fg)."""
        try:
            fcntl.ioctl(self.src, EVIOCGRAB, 1)
            print("[Device] Re-acquired EVIOCGRAB on resume", flush=True)
        except OSError:
            pass
            
        if KDE_INHERIT_TOUCHPAD_SETTINGS:
            threading.Thread(
                target=self.sync_kde_settings,
                args=(self.src_path, self.virtual_pointer_name),
                daemon=True,
            ).start()
            
        signal.signal(signal.SIGTSTP, self.handle_suspend)

    def reconnect(self) -> None:
        """Handle device disconnects by attempting auto-reconnect strictly for target_name."""
        print(f"[Device] Touchpad disconnected. Polling for '{self.target_name}'...", flush=True)

        try:
            fcntl.ioctl(self.src, EVIOCGRAB, 0)
        except OSError:
            pass

        try:
            os.close(self.src)
        except OSError:
            pass

        self.touches.clear()
        self.last_pos.clear()

        while True:
            time.sleep(2)
            try:
                new_path, _ = find_event_node(target_name=self.target_name)
                self.src = os.open(new_path, os.O_RDONLY | os.O_NONBLOCK)
                fcntl.ioctl(self.src, EVIOCGRAB, 1)
                self.src_path = new_path
                print(f"[Device] Reconnected successfully to '{self.target_name}' on {self.src_path}", flush=True)
                
                if KDE_INHERIT_TOUCHPAD_SETTINGS:
                    threading.Thread(
                        target=self.sync_kde_settings,
                        args=(self.src_path, self.virtual_pointer_name),
                        daemon=True,
                    ).start()
                    
                return
            except (OSError, FileNotFoundError):
                continue

    def run(self) -> None:
        """Main event loop monitoring the input node for read readiness."""
        print(
            f"touchpad-thumbkeys daemon running on {self.src_path} "
            f"(Max Res: {self.max_x}x{self.max_y}, Center X: {self.center_x})",
            flush=True,
        )
        while True:
            timeout = self.get_poll_timeout()
            ready = select.select([self.src], [], [], timeout)[0]
            
            if ready:
                try:
                    data = os.read(self.src, EVENT_STRUCT.size * 64)
                except BlockingIOError:
                    continue
                except OSError as e:
                    log(f"Device read error: {e}")
                    self.reconnect()
                    continue

                for off in range(0, len(data) - EVENT_STRUCT.size + 1, EVENT_STRUCT.size):
                    _, _, etype, code, value = EVENT_STRUCT.unpack_from(data, off)
                    self.handle_event(etype, code, value)
            else:
                self.process_held_keys()

    def shutdown(self, *_: object) -> None:
        """Release device locks, flush pending key releases, and destroy uinput nodes cleanly."""
        if getattr(self, "_is_shutting_down", False):
            return
        self._is_shutting_down = True

        print("\n[Exit] Shutting down touchpad-thumbkeys daemon...", flush=True)
        try:
            if hasattr(self, "touches") and hasattr(self, "kbd"):
                for t in self.touches.values():
                    if t.action_active and t.action:
                        self.trigger_action(t.action, 0)
                        print(f"=== Emergency ACTION UP on exit: {t.action} ===", flush=True)
                self.kbd.syn()

            if hasattr(self, "src"):
                fcntl.ioctl(self.src, EVIOCGRAB, 0)
                print("[Device] Released EVIOCGRAB lock", flush=True)
        except OSError:
            pass
        finally:
            if hasattr(self, "src"):
                try:
                    os.close(self.src)
                except OSError:
                    pass
            if hasattr(self, "kbd"):
                self.kbd.close()
            if hasattr(self, "pad"):
                self.pad.close()
            print("[Exit] Closed virtual uinput devices", flush=True)
            sys.exit(0)


# =============================================================================
# Main Execution Entry Point
# =============================================================================

if __name__ == "__main__":
    daemon = TouchZoneDaemon()

    atexit.register(daemon.shutdown)

    signal.signal(signal.SIGTERM, daemon.shutdown)
    signal.signal(signal.SIGINT, daemon.shutdown)
    signal.signal(signal.SIGTSTP, daemon.handle_suspend)
    signal.signal(signal.SIGCONT, daemon.handle_resume)

    daemon.run()