"""ScOFF backend: blank the Steam Deck screen through gamescope; wake on configurable controller input.

Mechanisms (verified on SteamOS 3.9.6 / gamescope 3.16.30.2):
  * Blank/unblank: gamescope ConVar `drm_sleep_internal_screen` via `gamescopectl`, which turns the
    eDP CRTC off. The result is read back from /sys/class/drm/card*-eDP-1/dpms.
  * Wake: raw HID input reports from the Steam Deck controller (28de:1205, interface 2). hidraw allows
    several readers, so this does not take input away from Steam. Report type 0x09 has a 64-bit
    button field at offset 8; the layout matches SDL's steamdeck driver.
  * Resume: logind's PrepareForSleep(false) signal, via `gdbus monitor`.
"""

import asyncio
import glob
import os
import struct
import time

import json

import decky

GAMESCOPE_ENV = {"XDG_RUNTIME_DIR": "/run/user/1000", "GAMESCOPE_WAYLAND_DISPLAY": "gamescope-0"}
CONVAR = "drm_sleep_internal_screen"

DECK_HID_ID = "000028DE:00001205"
DECK_GAMEPAD_INTERFACE = "02"
DECK_STATE_REPORT = 0x09

# Button bits (ulButtonsL | ulButtonsH << 32). The physically verified ones are marked (v).
B = {
    "R2": 0, "L2": 1, "R1": 2, "L1": 3, "Y": 4, "B": 5, "X": 6, "A": 7,        # B(v) A(v) L1(v) R1(v)
    "DPAD_UP": 8, "DPAD_RIGHT": 9, "DPAD_LEFT": 10, "DPAD_DOWN": 11,           # UP(v) LEFT(v) DOWN(v)
    "VIEW": 12, "STEAM": 13, "MENU": 14, "L5": 15, "R5": 16,                   # STEAM(v) L5(v)
    "LPAD_CLICK": 17, "RPAD_CLICK": 18, "LPAD_TOUCH": 19, "RPAD_TOUCH": 20,    # 17(v) 19(v) 20(v)
    "L3": 22, "R3": 26,
    "L4": 41, "R4": 42, "LSTICK_TOUCH": 46, "RSTICK_TOUCH": 47, "QAM": 50,     # L4 R4 46 47 QAM (v)
}
# Wake groups, each switchable in the settings. Stick touch (capacitive) never wakes: a thumb resting on a stick
# shouldn't count. Stick movement sets no button bits; it is read from the analog fields (see _analog_active).
BUTTON_GROUP = ["A", "B", "X", "Y", "DPAD_UP", "DPAD_RIGHT", "DPAD_LEFT", "DPAD_DOWN", "L1", "R1",
                "L4", "R4", "L5", "R5", "VIEW", "MENU", "STEAM", "QAM", "L3", "R3"]
TOUCHPAD_GROUP = ["LPAD_CLICK", "RPAD_CLICK", "LPAD_TOUCH", "RPAD_TOUCH"]
ANALOG_BUTTON_GROUP = ["L2", "R2"]  # full-pull bits of the analog triggers
BUTTON_MASK = sum(1 << B[k] for k in BUTTON_GROUP)
TOUCHPAD_MASK = sum(1 << B[k] for k in TOUCHPAD_GROUP)
# Analog inputs are mapped to virtual bits above the 64 real ones, so all inputs share one edge detector.
V = {"LSTICK": 64, "RSTICK": 65, "LTRIGGER": 66, "RTRIGGER": 67}
NAMES = {**{bit: name for name, bit in B.items()}, **{bit: name for name, bit in V.items()}}
ANALOG_MASK = sum(1 << B[k] for k in ANALOG_BUTTON_GROUP) | sum(1 << bit for bit in V.values())

# Report offsets (SDL steamdeck layout): u16 trigger L/R at 44/46, s16 stick LX/LY/RX/RY at 48..55.
# At rest the sticks read a few hundred (measured LX 434, LY 327, RX -79, RY 0).
STICK_THRESHOLD = 13000    # ~40 % deflection
TRIGGER_THRESHOLD = 8000   # ~25 % pull

SETTINGS_DEFAULTS = {
    "wake_buttons": True,      # face buttons, D-pad, bumpers, back buttons, View/Menu, Steam/QAM, stick clicks
    "wake_analog": False,      # stick movement and triggers
    "wake_touchpads": False,   # trackpad touch and click
    "power_double_tap": True,  # double-press power toggles the screen
    "power_long_press": True,  # holding power wakes the screen (with the power menu); off: the menu is blocked
}

# Presses within this window after blanking are ignored, so the press that triggered the blank doesn't count.
BLANK_GRACE_S = 0.4
# How long to wait for the wake button(s) to be released before telling the frontend to stop eating input.
RELEASE_TIMEOUT_S = 2.0
# Delay between logind's PrepareForSleep(false) and our unblank.
RESUME_UNBLANK_DELAY_S = 2.0

# Power key: read from the same evdev device steamos-powerbuttond uses (it doesn't grab it, so we can read it too).
POWER_KEYBOARD_NAME = "AT Translated Set 2 keyboard"
KEY_POWER = 116
# steamos-powerbuttond sends "longpowerpress" (power menu) after 1.0 s of holding (measured).
POWER_LONG_PRESS_S = 1.0
# A second press starting within this time after the first release counts as a double press. Measured double-tap:
# 175 ms hold, 88 ms gap. The frontend delays Steam's sleep by the same window (see src/index.tsx).
POWER_DOUBLE_WINDOW_S = 0.4


def _dpms() -> str:
    paths = glob.glob("/sys/class/drm/card*-eDP-1/dpms")
    if not paths:
        return "unknown"
    with open(paths[0]) as f:
        return f.read().strip()


def _find_deck_hidraw() -> str | None:
    for dev in sorted(glob.glob("/sys/class/hidraw/hidraw*")):
        try:
            with open(os.path.join(dev, "device", "uevent")) as f:
                if DECK_HID_ID not in f.read():
                    continue
            with open(os.path.join(dev, "device", "..", "bInterfaceNumber")) as f:
                if f.read().strip() != DECK_GAMEPAD_INTERFACE:
                    continue
        except OSError:
            continue
        return "/dev/" + os.path.basename(dev)
    return None


def _find_power_evdev() -> str | None:
    for dev in sorted(glob.glob("/sys/class/input/event*")):
        try:
            with open(os.path.join(dev, "device", "name")) as f:
                if f.read().strip() == POWER_KEYBOARD_NAME:
                    return "/dev/input/" + os.path.basename(dev)
        except OSError:
            continue
    return None


def _inputs(report: bytes) -> int | None:
    """Active inputs as a bit set: real button bits 0..63 plus virtual analog bits (V)."""
    if len(report) < 56 or report[2] != DECK_STATE_REPORT:
        return None
    lo, hi = struct.unpack_from("<II", report, 8)
    trig_l, trig_r = struct.unpack_from("<HH", report, 44)
    lx, ly, rx, ry = struct.unpack_from("<hhhh", report, 48)
    active = (hi << 32) | lo
    if max(abs(lx), abs(ly)) > STICK_THRESHOLD:
        active |= 1 << V["LSTICK"]
    if max(abs(rx), abs(ry)) > STICK_THRESHOLD:
        active |= 1 << V["RSTICK"]
    if trig_l > TRIGGER_THRESHOLD:
        active |= 1 << V["LTRIGGER"]
    if trig_r > TRIGGER_THRESHOLD:
        active |= 1 << V["RTRIGGER"]
    return active


class Plugin:
    # ---- gamescope -----------------------------------------------------------------------------

    async def _set_sleep(self, sleep: bool) -> bool:
        env = {**os.environ, **GAMESCOPE_ENV}
        try:
            proc = await asyncio.create_subprocess_exec(
                "gamescopectl", CONVAR, "1" if sleep else "0", env=env,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
            _, err = await asyncio.wait_for(proc.communicate(), timeout=5)
        except Exception as e:  # noqa: BLE001 - log and report failure
            decky.logger.error(f"gamescopectl failed: {e!r}")
            return False
        decky.logger.debug(f"gamescopectl {CONVAR} {'1' if sleep else '0'} rc={proc.returncode}")
        if proc.returncode != 0:
            decky.logger.error(f"gamescopectl rc={proc.returncode}: {err.decode(errors='replace').strip()}")
            return False
        want = "Off" if sleep else "On"
        for _ in range(20):  # the next atomic commit applies it; normally within a few frames
            if _dpms() == want:
                decky.logger.debug(f"dpms is {want}")
                return True
            await asyncio.sleep(0.05)
        decky.logger.warning(f"dpms did not become {want} (is {_dpms()})")
        return False

    async def _unblank_with_retry(self, attempts: int = 10, delay: float = 1.0) -> bool:
        for i in range(attempts):
            if await self._set_sleep(False):
                return True
            decky.logger.info(f"unblank attempt {i + 1} failed, retrying")
            await asyncio.sleep(delay)
        return False

    # ---- hidraw wake watcher -------------------------------------------------------------------

    def _stop_watcher(self):
        if self.hid_fd is not None:
            try:
                self.loop.remove_reader(self.hid_fd)
            except Exception:  # noqa: BLE001
                pass
            os.close(self.hid_fd)
            self.hid_fd = None

    def _on_hid_readable(self):
        try:
            report = os.read(self.hid_fd, 64)
        except BlockingIOError:
            return
        except OSError as e:
            decky.logger.error(f"hidraw read failed: {e!r}; unblanking")
            self._stop_watcher()
            self.loop.create_task(self._wake("hid-error"))
            return
        active = _inputs(report)
        if active is None:
            return
        mask = self._wake_mask()
        if self.waking:
            # Waiting for the wake input to be released.
            if active & mask == 0:
                self.released.set()
            return
        pressed = active & ~self.prev_buttons & mask
        self.prev_buttons = active
        if pressed and time.monotonic() - self.blanked_at > BLANK_GRACE_S:
            names = [NAMES[bit] for bit in range(pressed.bit_length()) if pressed >> bit & 1]
            self.waking = True
            self.loop.create_task(self._wake("input:" + "+".join(names)))

    def _wake_mask(self) -> int:
        st = self.settings
        return ((BUTTON_MASK if st["wake_buttons"] else 0) | (TOUCHPAD_MASK if st["wake_touchpads"] else 0)
                | (ANALOG_MASK if st["wake_analog"] else 0))

    # ---- settings ------------------------------------------------------------------------------

    def _settings_path(self) -> str:
        return os.path.join(decky.DECKY_PLUGIN_SETTINGS_DIR, "settings.json")

    def _load_settings(self):
        self.settings = dict(SETTINGS_DEFAULTS)
        try:
            with open(self._settings_path()) as f:
                saved = json.load(f)
            self.settings.update({k: bool(v) for k, v in saved.items() if k in SETTINGS_DEFAULTS})
        except FileNotFoundError:
            pass
        except Exception as e:  # noqa: BLE001
            decky.logger.error(f"could not read settings, using defaults: {e!r}")
        decky.logger.debug(f"settings: {self.settings}")

    def _save_settings(self):
        os.makedirs(decky.DECKY_PLUGIN_SETTINGS_DIR, exist_ok=True)
        tmp = self._settings_path() + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.settings, f, indent=2)
        os.replace(tmp, self._settings_path())

    def _can_wake(self) -> bool:
        return any(self.settings.values())

    async def _wake(self, reason: str):
        decky.logger.info(f"waking: {reason}")
        ok = await self._unblank_with_retry(attempts=3, delay=0.5)
        if self.hid_fd is not None and reason.startswith("input"):
            # Keep the frontend eating input until the wake press is released, so it doesn't leak into the UI.
            try:
                await asyncio.wait_for(self.released.wait(), timeout=RELEASE_TIMEOUT_S)
            except asyncio.TimeoutError:
                pass
        self._stop_watcher()
        self.blanked = False
        self.waking = False
        decky.logger.info(f"woke ({reason}) ok={ok} dpms={_dpms()}")
        await decky.emit("scoff_woke", reason, ok)

    # ---- logind resume monitor -----------------------------------------------------------------

    async def _resume_monitor(self):
        while True:
            try:
                # stdbuf -oL: gdbus writes to a pipe, which is block-buffered by default and delays signals.
                proc = await asyncio.create_subprocess_exec(
                    "stdbuf", "-oL", "gdbus", "monitor", "--system", "--dest", "org.freedesktop.login1",
                    "--object-path", "/org/freedesktop/login1",
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
                self.monitor_proc = proc
                decky.logger.debug("resume monitor started")
                async for line in proc.stdout:
                    if b"PrepareForSleep (true,)" in line:
                        self.loop.create_task(self._on_sleep())
                    elif b"PrepareForSleep (false,)" in line:
                        self.loop.create_task(self._on_resume())
                await proc.wait()
                decky.logger.warning(f"gdbus monitor exited rc={proc.returncode}; restarting")
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                decky.logger.error(f"resume monitor error: {e!r}")
            await asyncio.sleep(2)

    async def _on_sleep(self):
        # Deliberately do NOT touch the screen here. gamescope's own logind handler (src/logind_sleep.cpp)
        # records each screen's sleep state, sleeps the screens, and restores the recorded state on resume.
        # Unblanking at the same moment raced with it and left the panel lit with no image (plugin test 2).
        decky.logger.debug(f"going to sleep: blanked={self.blanked} dpms={_dpms()}")
        if self.blanked:
            self.resume_pending = True

    async def _on_resume(self):
        decky.logger.debug(f"resumed: blanked={self.blanked} pending={self.resume_pending} dpms={_dpms()}")
        if not (self.blanked or self.resume_pending or _dpms() == "Off"):
            return
        # gamescope restores the recorded "asleep" state when it gets the same signal. Wait until that has
        # happened (and the post-resume modeset settled) before unblanking, or the restore wins (run 2).
        await asyncio.sleep(RESUME_UNBLANK_DELAY_S)
        ok = await self._unblank_with_retry()
        decky.logger.info(f"post-resume unblank ok={ok} dpms={_dpms()}")
        self._stop_watcher()
        self.blanked = False
        self.waking = False
        self.resume_pending = False
        await decky.emit("scoff_woke", "resume", ok)
        await asyncio.sleep(3)
        decky.logger.debug(f"post-resume recheck dpms={_dpms()}")
        if _dpms() == "Off":
            ok = await self._unblank_with_retry()
            decky.logger.info(f"post-resume second unblank ok={ok} dpms={_dpms()}")

    # ---- power key watcher (long press wakes; double press toggles) -----------------------------

    def _start_power_watcher(self):
        path = _find_power_evdev()
        if path is None:
            decky.logger.warning("power key device not found; long/double press disabled")
            return
        try:
            self.power_fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        except OSError as e:
            decky.logger.warning(f"cannot open {path}: {e.strerror}; long/double press disabled")
            return
        self.loop.add_reader(self.power_fd, self._on_power_readable)
        decky.logger.debug(f"power key watcher on {path}")

    def _stop_power_watcher(self):
        if self.power_fd is not None:
            try:
                self.loop.remove_reader(self.power_fd)
            except Exception:  # noqa: BLE001
                pass
            os.close(self.power_fd)
            self.power_fd = None

    def _on_power_readable(self):
        try:
            data = os.read(self.power_fd, 24 * 32)
        except BlockingIOError:
            return
        except OSError as e:
            decky.logger.error(f"power key read failed: {e!r}")
            self._stop_power_watcher()
            return
        for i in range(0, len(data) - 23, 24):
            sec, usec, typ, code, val = struct.unpack_from("llHHi", data, i)
            if typ != 1 or code != KEY_POWER or val == 2:  # EV_KEY only; ignore autorepeat
                continue
            t = sec + usec / 1e6
            if val == 1:
                self._power_down(t)
            else:
                self._power_up(t)

    def _power_down(self, t: float):
        if self.power_long_task:
            self.power_long_task.cancel()
        self.power_long_task = self.loop.create_task(self._power_long_check())
        if self.settings["power_double_tap"] and self.power_last_up is not None \
                and t - self.power_last_up <= POWER_DOUBLE_WINDOW_S and self.power_last_short:
            self.power_last_up = None  # don't chain a third press into another double
            self.loop.create_task(self._power_double())
        self.power_down_at = t

    def _power_up(self, t: float):
        if self.power_long_task:
            self.power_long_task.cancel()
            self.power_long_task = None
        held = t - self.power_down_at if self.power_down_at is not None else 0.0
        self.power_last_short = held < POWER_LONG_PRESS_S
        self.power_last_up = t
        self.power_down_at = None

    async def _power_long_check(self):
        await asyncio.sleep(POWER_LONG_PRESS_S)
        decky.logger.debug(f"power long press (blanked={self.blanked})")
        if self.blanked and not self.waking and self.settings["power_long_press"]:
            # Steam is opening the power menu now; show it, and stop eating input so it can be used.
            # (With the setting off, the frontend blocks the menu while the screen is off.)
            self.waking = True
            await self._wake("power:long")

    async def _power_double(self):
        action = "wake" if self.blanked else "blank"
        decky.logger.info(f"power double press -> {action}")
        # The frontend cancels the sleep that Steam started for the first press, and (for "blank") turns the screen off.
        await decky.emit("scoff_power_double", action)
        if action == "wake" and not self.waking:
            self.waking = True
            await self._wake("power:double")

    # ---- frontend API --------------------------------------------------------------------------

    async def blank(self) -> dict:
        if self.blanked:
            return {"ok": True, "msg": "already off"}
        if not self._can_wake():
            return {"ok": False, "msg": "no wake method is enabled in the settings"}
        hid = _find_deck_hidraw()
        if hid is None:
            return {"ok": False, "msg": "Steam Deck controller hidraw not found; refusing to blank without a wake path"}
        try:
            self.hid_fd = os.open(hid, os.O_RDONLY | os.O_NONBLOCK)
        except OSError as e:
            return {"ok": False, "msg": f"cannot open {hid}: {e.strerror}"}
        self.prev_buttons = 0
        self.waking = False
        self.released = asyncio.Event()
        self.blanked_at = time.monotonic()
        self.loop.add_reader(self.hid_fd, self._on_hid_readable)
        if not await self._set_sleep(True):
            self._stop_watcher()
            await self._set_sleep(False)
            return {"ok": False, "msg": "gamescope did not turn the screen off"}
        self.blanked = True
        decky.logger.info(f"screen off (wake watcher on {hid})")
        return {"ok": True, "msg": "off"}

    async def unblank(self) -> dict:
        ok = await self._unblank_with_retry(attempts=3, delay=0.5)
        self._stop_watcher()
        self.blanked = False
        self.waking = False
        return {"ok": ok}

    async def get_settings(self) -> dict:
        return self.settings

    async def set_setting(self, key: str, value: bool) -> dict:
        if key not in SETTINGS_DEFAULTS:
            raise ValueError(f"unknown setting {key!r}")
        self.settings[key] = bool(value)
        self._save_settings()
        decky.logger.info(f"setting {key}={self.settings[key]}")
        return self.settings

    async def status(self) -> dict:
        return {"blanked": self.blanked, "dpms": _dpms(), "hidraw": _find_deck_hidraw()}

    # ---- lifecycle -----------------------------------------------------------------------------

    async def _main(self):
        self.loop = asyncio.get_running_loop()
        self.blanked = False
        self.waking = False
        self.hid_fd = None
        self.prev_buttons = 0
        self.blanked_at = 0.0
        self.released = asyncio.Event()
        self.resume_pending = False
        self.power_fd = None
        self.power_down_at = None
        self.power_last_up = None
        self.power_last_short = False
        self.power_long_task = None
        self.monitor_proc = None
        self._load_settings()
        self.monitor_task = self.loop.create_task(self._resume_monitor())
        self._start_power_watcher()
        decky.logger.info("ScOFF backend started")

    async def _unload(self):
        self.monitor_task.cancel()
        self._stop_power_watcher()
        if self.monitor_proc and self.monitor_proc.returncode is None:
            self.monitor_proc.kill()
        if self.blanked or _dpms() == "Off":
            await self._unblank_with_retry(attempts=3, delay=0.5)
        self._stop_watcher()
        decky.logger.info("ScOFF backend unloaded")

    async def _uninstall(self):
        pass
