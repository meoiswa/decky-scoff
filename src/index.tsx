import {
  ButtonItem,
  Focusable,
  PanelSection,
  PanelSectionRow,
  showModal,
  staticClasses,
  ToggleField,
} from "@decky/ui";
import { addEventListener, callable, definePlugin, removeEventListener, toaster } from "@decky/api";
import { useEffect, useState } from "react";
import { FaPowerOff } from "react-icons/fa";

type Result = { ok: boolean; msg?: string };
const blank = callable<[], Result>("blank");
const unblank = callable<[], Result>("unblank");

type Settings = {
  wake_buttons: boolean;
  wake_analog: boolean;
  wake_touchpads: boolean;
  power_double_tap: boolean;
  power_long_press: boolean;
};
const getSettings = callable<[], Settings>("get_settings");
const setSetting = callable<[key: keyof Settings, value: boolean], Settings>("set_setting");

// Module-level copy: the sleep hook needs it while the panel isn't mounted.
let settings: Settings | null = null;
const settingsListeners = new Set<(s: Settings) => void>();
function applySettings(s: Settings) {
  settings = s;
  settingsListeners.forEach((l) => l(s));
}
const anyWakeMethod = (s: Settings | null) =>
  !!s && (s.wake_buttons || s.wake_analog || s.wake_touchpads || s.power_double_tap || s.power_long_press);

// ---- Input eating ------------------------------------------------------------------------------
// While the screen is off, nothing should reach Steam or the running game. This copies what Steam's
// own idle status screen does (low-power download mode):
//   1. MenuStore.SetSuppressMenus(): the Steam and QAM buttons can't open their side menus.
//   2. Composition request "Opaque" (3): Steam UI is composited on top and has input focus, even over a game.
//   3. A full-screen, autofocused Focusable that swallows every button and direction.
// The state lives at module level because the QAM panel unmounts as soon as menus are suppressed.

const COMPOSITION_OPAQUE = 3;
const COMPOSITION_KEY = "ScoffScreenOff";

// eslint-disable-next-line @typescript-eslint/no-explicit-any
const mainWindow = (): any => (window as any).SteamUIStore?.WindowStore?.GamepadUIMainWindowInstance;

let eating: { unsuppress?: () => void; composition: boolean; modal?: { Close: () => void } } | null = null;
const listeners = new Set<(active: boolean) => void>();
const notify = () => listeners.forEach((l) => l(eating !== null));

const swallow = () => true; // returning true marks the gamepad event as handled

function Eater() {
  return (
    <Focusable
      autoFocus
      noFocusRing
      onButtonDown={swallow}
      onButtonUp={swallow}
      onGamepadDirection={swallow}
      onOKButton={swallow}
      onCancelButton={swallow}
      onSecondaryButton={swallow}
      onOptionsButton={swallow}
      onMenuButton={swallow}
      style={{
        position: "fixed",
        inset: 0,
        background: "black",
        zIndex: 10000,
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        color: "#333",
        fontSize: "16px",
      }}
    >
      <div>Screen off. Press any button to wake.</div>
    </Focusable>
  );
}

function startEating() {
  if (eating) return;
  const win = mainWindow();
  eating = { composition: false };
  try {
    eating.unsuppress = win?.MenuStore?.SetSuppressMenus?.();
  } catch (e) {
    console.error("[scoff] SetSuppressMenus failed", e);
  }
  try {
    win?.m_CompositionStateStore?.AddMinimumCompositionStateRequest(COMPOSITION_OPAQUE, COMPOSITION_KEY);
    eating.composition = true;
  } catch (e) {
    console.error("[scoff] composition request failed", e);
  }
  eating.modal = showModal(<Eater />, undefined, { strTitle: "ScOFF", bNeverPopOut: true, bHideActionIcons: true });
  notify();
}

function stopEating() {
  if (!eating) return;
  const e = eating;
  eating = null;
  try {
    e.modal?.Close();
  } catch (err) {
    console.error("[scoff] modal close failed", err);
  }
  if (e.composition) {
    try {
      mainWindow()?.m_CompositionStateStore?.RemoveMinimumCompositionStateRequest(COMPOSITION_OPAQUE, COMPOSITION_KEY);
    } catch (err) {
      console.error("[scoff] composition release failed", err);
    }
  }
  try {
    e.unsuppress?.();
  } catch (err) {
    console.error("[scoff] unsuppress failed", err);
  }
  notify();
}

async function screenOff() {
  startEating();
  let res: Result;
  try {
    res = await blank();
  } catch (e) {
    res = { ok: false, msg: String(e) };
  }
  if (!res.ok) {
    stopEating();
    toaster.toast({ title: "ScOFF", body: `Could not turn the screen off: ${res.msg ?? "unknown error"}` });
  }
}

// ---- Power double press ------------------------------------------------------------------------
// Each short power press reaches Steam as a suspend request: SuspendResumeStore.OnRequestSuspend (a pre-bound
// copy registered for the notification) calls this.InitiateSleep(), which is looked up on the instance. We
// put an own-property wrapper there that holds the sleep for POWER_DOUBLE_WINDOW_MS. If the backend reports a
// double press in that window, the pending sleep is cancelled; otherwise it goes ahead as normal.
// The backend uses the same window (main.py POWER_DOUBLE_WINDOW_S).

const POWER_DOUBLE_WINDOW_MS = 400;
// After a double press, also drop the sleep request that comes from the second press's release.
const POWER_DOUBLE_SUPPRESS_MS = 1500;

let pendingSleep: ReturnType<typeof setTimeout> | null = null;
let suppressSleepUntil = 0;

function installSleepHook() {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const store = (window as any).SuspendResumeStore;
  const proto = store && Object.getPrototypeOf(store);
  if (!store || typeof proto?.InitiateSleep !== "function") {
    console.warn("[scoff] SuspendResumeStore.InitiateSleep not found; power double press disabled");
    return () => {};
  }
  const original = proto.InitiateSleep as (arg?: unknown) => unknown;
  store.InitiateSleep = function (arg?: unknown): unknown {
    if (Date.now() < suppressSleepUntil) {
      console.log("[scoff] sleep request dropped (part of a power double press)");
      return undefined;
    }
    if (!settings?.power_double_tap) return original.call(store, arg); // feature off: no added delay
    if (pendingSleep) return undefined; // a sleep is already queued; Steam would ignore the duplicate anyway
    pendingSleep = setTimeout(() => {
      pendingSleep = null;
      original.call(store, arg);
    }, POWER_DOUBLE_WINDOW_MS);
    return undefined;
  };
  return () => {
    if (pendingSleep) {
      // Don't lose a sleep that was requested just before unloading.
      clearTimeout(pendingSleep);
      pendingSleep = null;
      original.call(store);
    }
    delete store.InitiateSleep; // back to the prototype method
  };
}

// While the screen is off with "Hold power to wake" disabled, block the power menu. A long press reaches the UI as
// the service notification "SleepManager.NotifyShowPowerMenu#1"; the handler registry stores it as an entry
// whose `invoke` it calls on dispatch. We wrap `invoke` so the menu (and its sound) never starts.
// (SteamUIStore.OpenPowerMenu itself is a non-writable, non-configurable property, so it can't be wrapped.)

const POWER_MENU_NOTIFICATION = "SleepManager.NotifyShowPowerMenu#1";

type HandlerEntry = { invoke: (...args: unknown[]) => unknown };
type HandlerRegistry = { m_mapServiceMethodHandlers: Map<string, HandlerEntry[]> };

function findHandlerRegistry(): HandlerRegistry | null {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const chunks = (window as any).webpackChunksteamui;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  let req: any = null;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  chunks?.push([[Symbol("scoff")], {}, (r: any) => (req = r)]);
  if (!req) return null;
  for (const id of Object.keys(req.m)) {
    const src = Function.prototype.toString.call(req.m[id]);
    if (!src.includes("GetDefaultHandlerRegistry(){") || !src.includes("SetDefaultHandlerRegistry")) continue;
    let mod;
    try {
      mod = req(id);
    } catch {
      continue;
    }
    for (const k of Object.keys(mod ?? {})) {
      const getter = mod[k];
      if (typeof getter !== "function" || getter.length !== 0) continue;
      try {
        const reg = getter()?.GetDefaultHandlerRegistry?.();
        if (reg?.m_mapServiceMethodHandlers instanceof Map) return reg;
      } catch {
        // not the context getter
      }
    }
  }
  return null;
}

function installPowerMenuHook() {
  const entries = findHandlerRegistry()?.m_mapServiceMethodHandlers.get(POWER_MENU_NOTIFICATION);
  if (!entries?.length) {
    console.warn("[scoff] power menu notification handler not found; cannot block the power menu");
    return () => {};
  }
  const originals = entries.map((e) => e.invoke);
  entries.forEach((e, i) => {
    e.invoke = function (this: unknown, ...args: unknown[]) {
      if (eating && settings && !settings.power_long_press) {
        console.log("[scoff] power menu blocked while the screen is off");
        return undefined;
      }
      return originals[i].apply(this, args);
    };
  });
  return () => entries.forEach((e, i) => (e.invoke = originals[i]));
}

function onPowerDouble(action: string) {
  suppressSleepUntil = Date.now() + POWER_DOUBLE_SUPPRESS_MS;
  if (pendingSleep) {
    clearTimeout(pendingSleep);
    pendingSleep = null;
  }
  if (action === "blank") screenOff(); // "wake" is handled by the backend, which then emits scoff_woke
}

// ---- Panel -------------------------------------------------------------------------------------

const TOGGLES: { key: keyof Settings; label: string; description: string }[] = [
  {
    key: "power_long_press",
    label: "Hold power to wake",
    description: "Wakes the screen with the power menu open. When off, the power menu is blocked while the screen is off.",
  },
  {
    key: "wake_buttons",
    label: "Buttons wake the screen",
    description: "Face buttons, D-pad, bumpers, back buttons, View/Menu, Steam/QAM and stick clicks.",
  },
  { key: "wake_analog", label: "Sticks and triggers wake the screen", description: "Moving a stick or pulling a trigger." },
  {
    key: "wake_touchpads",
    label: "Trackpads wake the screen",
    description: "Touching or clicking either trackpad.",
  },
  {
    key: "power_double_tap",
    label: "Double-press power to toggle",
    description: "Turns the screen off or on without sleeping. Adds 0.4 s before a single press sleeps.",
  },
];

function Content() {
  const [active, setActive] = useState(eating !== null);
  const [cfg, setCfg] = useState<Settings | null>(settings);
  useEffect(() => {
    listeners.add(setActive);
    settingsListeners.add(setCfg);
    getSettings().then(applySettings);
    return () => {
      listeners.delete(setActive);
      settingsListeners.delete(setCfg);
    };
  }, []);
  const canWake = anyWakeMethod(cfg);
  return (
    <>
      <PanelSection>
        <PanelSectionRow>
          <ButtonItem layout="below" disabled={active || !canWake} onClick={screenOff}>
            Turn screen off
          </ButtonItem>
        </PanelSectionRow>
        {!canWake && (
          <PanelSectionRow>
            <div style={{ fontSize: "12px", opacity: 0.7 }}>Enable at least one way to wake the screen below.</div>
          </PanelSectionRow>
        )}
      </PanelSection>
      <PanelSection title="Settings">
        {TOGGLES.map((t) => (
          <PanelSectionRow key={t.key}>
            <ToggleField
              label={t.label}
              description={t.description}
              checked={cfg?.[t.key] ?? false}
              disabled={!cfg}
              onChange={async (value) => applySettings(await setSetting(t.key, value))}
            />
          </PanelSectionRow>
        ))}
      </PanelSection>
    </>
  );
}

export default definePlugin(() => {
  getSettings().then(applySettings).catch((e) => console.error("[scoff] get_settings failed", e));
  const removeSleepHook = installSleepHook();
  const removePowerMenuHook = installPowerMenuHook();
  const powerDouble = addEventListener<[action: string]>("scoff_power_double", onPowerDouble);

  const woke = addEventListener<[reason: string, ok: boolean]>("scoff_woke", (reason, ok) => {
    console.log("[scoff] woke:", reason, ok);
    stopEating();
    if (!ok) toaster.toast({ title: "ScOFF", body: "The screen may not have turned back on. Try toggling it again." });
  });

  return {
    name: "ScOFF",
    titleView: <div className={staticClasses.Title}>ScOFF</div>,
    content: <Content />,
    icon: <FaPowerOff />,
    onDismount() {
      removeEventListener("scoff_woke", woke);
      removeEventListener("scoff_power_double", powerDouble);
      removeSleepHook();
      removePowerMenuHook();
      if (eating) {
        stopEating();
        unblank();
      }
    },
  };
});
