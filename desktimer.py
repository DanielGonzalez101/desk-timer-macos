#!/usr/bin/env python3
# desktimer.py — DeskTimer para macOS
# App de barra de menú que cronometra automáticamente el tiempo en cada Space de macOS.
# Requiere: pip install rumps pyobjc-framework-Cocoa pyobjc-framework-Quartz

import rumps
import time
import datetime
import json
import os
import threading
import ctypes
import ctypes.util
import uuid
import signal
import logging

from AppKit import (
    NSWorkspace, NSObject, NSSound, NSView, NSTextField, NSButton,
    NSFont, NSColor, NSMakeRect, NSMenu, NSMenuItem, NSStatusBar,
    NSVariableStatusItemLength,
)
import Quartz

# ─── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler()],
)
log = logging.getLogger("desktimer")

# ─── Constantes ────────────────────────────────────────────────────────────────
DATA_DIR      = os.path.expanduser("~/.desktimer")
HISTORY_FILE  = os.path.join(DATA_DIR, "space_history.json")
NAMES_FILE    = os.path.join(DATA_DIR, "space_names.json")
GOALS_FILE    = os.path.join(DATA_DIR, "space_goals.json")
TASKS_FILE    = os.path.join(DATA_DIR, "space_tasks.json")
SETTINGS_FILE  = os.path.join(DATA_DIR, "settings.json")
PINNED_FILE    = os.path.join(DATA_DIR, "pinned.json")
NUMBERS_FILE   = os.path.join(DATA_DIR, "space_numbers.json")   # sid → número secuencial
DELETED_FILE   = os.path.join(DATA_DIR, "deleted_spaces.json")  # [sid, ...]

IDLE_THRESHOLD   = 200   # segundos → pausa automática
IDLE_RESUME_AT   = 30    # segundos → reanudar si volvió el usuario
TICK_INTERVAL    = 1     # segundos entre ticks del timer
AUTOSAVE_INTERVAL = 30   # segundos entre autosaves

TASK_ROW_WIDTH  = 280
TASK_ROW_HEIGHT = 22

DEFAULT_SETTINGS = {
    "pomo_focus": 1500,
    "pomo_break": 300,
    "pomo_long_break": 900,
    "pomo_sessions_for_long": 4,
    "idle_threshold": IDLE_THRESHOLD,
    "idle_resume": IDLE_RESUME_AT,
}

# ─── CoreGraphics (API privada) ────────────────────────────────────────────────

def _get_ordered_space_ids():
    """Devuelve los IDs de spaces en orden visual usando CGSCopyManagedDisplaySpaces."""
    try:
        from Foundation import NSBundle
        import objc as _objc
        _bundle = NSBundle.bundleWithPath_("/System/Library/Frameworks/CoreGraphics.framework")
        ns = {}
        _objc.loadBundleFunctions(_bundle, ns, [("CGSCopyManagedDisplaySpaces", b"@i")])
        fn = ns.get("CGSCopyManagedDisplaySpaces")
        if not fn:
            return []
        _cg_tmp = ctypes.cdll.LoadLibrary(ctypes.util.find_library("CoreGraphics"))
        _cg_tmp.CGSMainConnectionID.restype = ctypes.c_int
        _cg_tmp.CGSMainConnectionID.argtypes = []
        conn = _cg_tmp.CGSMainConnectionID()
        display_spaces = fn(conn)
        result = []
        for display_dict in (display_spaces or []):
            for space in display_dict.get("Spaces", []):
                sid = space.get("ManagedSpaceID") or space.get("id64")
                if sid is not None:
                    result.append(int(sid))
        return result
    except Exception as e:
        log.warning(f"_get_ordered_space_ids: {e}")
        return []

def _init_cgs():
    try:
        cg = ctypes.cdll.LoadLibrary(ctypes.util.find_library("CoreGraphics"))
        cg.CGSMainConnectionID.restype = ctypes.c_int
        cg.CGSMainConnectionID.argtypes = []
        cg.CGSGetActiveSpace.restype = ctypes.c_uint64
        cg.CGSGetActiveSpace.argtypes = [ctypes.c_int]
        return cg
    except Exception as e:
        log.error(f"No se pudo cargar CoreGraphics: {e}")
        return None

_cg = _init_cgs()

def get_current_space_id():
    if _cg is None:
        return 1
    try:
        conn = _cg.CGSMainConnectionID()
        return int(_cg.CGSGetActiveSpace(conn))
    except Exception as e:
        log.error(f"get_current_space_id fallo: {e}")
        return 1

# ─── Helpers ───────────────────────────────────────────────────────────────────

def fmt_time(seconds):
    """Formatea segundos como H:MM:SS."""
    total = max(0, int(seconds))
    h = total // 3600
    m = (total % 3600) // 60
    s = total % 60
    return f"{h}:{m:02d}:{s:02d}"

def get_idle_seconds():
    """Retorna los segundos de inactividad del sistema."""
    try:
        return Quartz.CGEventSourceSecondsSinceLastEventType(
            Quartz.kCGEventSourceStateCombinedSessionState,
            Quartz.kCGAnyInputEventType,
        )
    except Exception:
        return 0.0

def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        if not isinstance(e, FileNotFoundError):
            log.warning(f"JSON corrupto en {path}, iniciando vacio")
        return default

def save_json(path, data):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log.error(f"Error guardando {path}: {e}")

def play_sound(name="Glass"):
    try:
        sound = NSSound.soundNamed_(name)
        if sound:
            sound.play()
    except Exception as e:
        log.warning(f"Error reproduciendo sonido {name}: {e}")

def set_tooltip(rumps_item, text):
    """Helper: establece tooltip en un NSMenuItem de rumps."""
    try:
        rumps_item._menuitem.setToolTip_(text)
    except Exception as e:
        log.warning(f"set_tooltip fallo: {e}")

def truncate(text, max_len=22):
    """Trunca texto con elipsis si excede max_len."""
    if len(text) <= max_len:
        return text
    return text[:max_len - 1] + "…"

# ─── Observer de Spaces ────────────────────────────────────────────────────────

class SpaceObserver(NSObject):
    app_ref = None  # se asigna antes de registrar

    def spaceDidChange_(self, notification):
        if self.app_ref:
            self.app_ref.handle_space_change()

    def systemDidWake_(self, notification):
        if self.app_ref:
            self.app_ref.handle_wake()

# ─── Custom NSView: TaskRowView ────────────────────────────────────────────────

class TaskRowView(NSView):
    """Fila de tarea embebida en NSMenuItem con botones de pin, check, texto y eliminar."""

    _app_ref   = None
    _task_id   = None
    _is_done   = False
    _pin_index = None   # None = no pinned, int (0-based) = position in pin list

    @classmethod
    def rowForTask_app_pinIndex_(cls, task, app, pin_index):
        """
        pin_index: None if not pinned, else 0-based index in _pinned[sid] list.
        """
        width  = TASK_ROW_WIDTH
        height = TASK_ROW_HEIGHT
        view = cls.alloc().initWithFrame_(NSMakeRect(0, 0, width, height))
        view._app_ref   = app
        view._task_id   = task["id"]
        view._is_done   = task.get("done", False)
        view._pin_index = pin_index

        # ── Pin button (x=4, w=20, h=18) ──
        pin_btn = NSButton.alloc().initWithFrame_(NSMakeRect(4, 2, 20, 18))
        if pin_index is not None:
            pin_btn.setTitle_(str(pin_index + 1))  # 1-based
        else:
            pin_btn.setTitle_("·")
        pin_btn.setBordered_(False)
        pin_btn.setFont_(NSFont.systemFontOfSize_(12))
        pin_btn.setTarget_(view)
        pin_btn.setAction_("onPin:")
        view.addSubview_(pin_btn)
        view._pin_btn = pin_btn

        # ── Check toggle (x=26, w=20, h=18) ──
        chk_btn = NSButton.alloc().initWithFrame_(NSMakeRect(26, 2, 20, 18))
        chk_btn.setTitle_("✓" if view._is_done else "○")
        chk_btn.setFont_(NSFont.systemFontOfSize_(12))
        chk_btn.setBordered_(False)
        chk_btn.setTarget_(view)
        chk_btn.setAction_("onToggle:")
        view.addSubview_(chk_btn)
        view._chk_btn = chk_btn

        # ── Text field: no editable por defecto para no bloquear el cierre del menú ──
        tf = NSTextField.alloc().initWithFrame_(NSMakeRect(48, 3, 188, 16))
        tf.setStringValue_(task.get("text", ""))
        tf.setEditable_(False)
        tf.setSelectable_(False)
        tf.setBordered_(False)
        tf.setBackgroundColor_(NSColor.clearColor())
        tf.setFont_(NSFont.systemFontOfSize_(12))
        tf.setTextColor_(NSColor.secondaryLabelColor() if view._is_done else NSColor.labelColor())
        view.addSubview_(tf)
        view._text_field = tf

        # ── Delete button (x=238, w=20, h=18) ──
        del_btn = NSButton.alloc().initWithFrame_(NSMakeRect(238, 2, 20, 18))
        del_btn.setTitle_("✕")
        del_btn.setBordered_(False)
        del_btn.setFont_(NSFont.systemFontOfSize_(11))
        del_btn.setTarget_(view)
        del_btn.setAction_("onDelete:")
        view.addSubview_(del_btn)
        view._del_btn = del_btn

        return view

    def onPin_(self, sender):
        if self._app_ref and self._task_id:
            self._app_ref.toggle_task_pin(self._task_id)

    def onToggle_(self, sender):
        if self._app_ref and self._task_id:
            self._app_ref.toggle_task_done(self._task_id)

    def onEditDone_(self, sender):
        if self._app_ref and self._task_id:
            new_text = str(self._text_field.stringValue()).strip()
            if new_text:
                self._app_ref.rename_task(self._task_id, new_text)

    def onDelete_(self, sender):
        if self._app_ref and self._task_id:
            self._app_ref.delete_task(self._task_id)

    def acceptsFirstMouse_(self, event):
        return True

# ─── Custom NSView: AddTaskView ────────────────────────────────────────────────

class AddTaskView(NSView):
    """Campo inline para agregar tarea. Solo Enter confirma."""

    _app_ref = None

    @classmethod
    def viewForApp_(cls, app):
        view = cls.alloc().initWithFrame_(NSMakeRect(0, 0, TASK_ROW_WIDTH, 26))
        view._app_ref = app

        tf = NSTextField.alloc().initWithFrame_(NSMakeRect(10, 4, TASK_ROW_WIDTH - 18, 18))
        tf.setPlaceholderString_("Nueva tarea...  (Enter)")
        tf.setEditable_(True)
        tf.setBordered_(False)
        tf.setBackgroundColor_(NSColor.clearColor())
        tf.setFont_(NSFont.systemFontOfSize_(12))
        tf.setTextColor_(NSColor.labelColor())
        tf.cell().setScrollable_(True)
        tf.cell().setWraps_(False)
        tf.setTarget_(view)
        tf.setAction_("onAdd:")
        view.addSubview_(tf)
        view._text_field = tf
        return view

    # Necesario para que el campo reciba foco al hacer click en el menú
    def acceptsFirstMouse_(self, event):
        return True

    def mouseDown_(self, event):
        win = self.window()
        if win:
            win.makeFirstResponder_(self._text_field)

    def onAdd_(self, sender):
        text = str(sender.stringValue()).strip()
        if text and self._app_ref:
            self._app_ref.add_task_from_text(text)

# ─── App principal ─────────────────────────────────────────────────────────────

class DeskTimerApp(rumps.App):

    def __init__(self):
        super().__init__("Cargando...", quit_button=None)

        os.makedirs(DATA_DIR, exist_ok=True)

        # ── Estado del timer ──
        self._lock = threading.Lock()
        self._current_space = None
        self._space_seconds = {}        # {space_id_str: float}
        self._session_start = time.time()
        self._is_paused = False
        self._is_idle_paused = False
        self._today = datetime.date.today()
        self._last_space_check = None   # para fallback de polling

        # ── Estado Pomodoro ──
        self._pomo_lock = threading.Lock()
        self._pomo_active = False
        self._pomo_paused = False
        self._pomo_phase = "focus"
        self._pomo_remaining = DEFAULT_SETTINGS["pomo_focus"]
        self._pomo_last_tick = None
        self._pomo_sessions = 0
        self._pomo_was_active = False   # para restaurar tras pausa global

        # ── Estado Tareas ──
        self._task_lock = threading.Lock()
        self._tasks = {}                # {space_id_str: [task_dict, ...]}

        # ── Estado Pin ──
        # _pinned = {sid: [task_id1, task_id2, ...]} (ordered list)
        self._pinned = {}

        # ── Cargar datos ──
        self._settings      = load_json(SETTINGS_FILE, DEFAULT_SETTINGS.copy())
        self._names         = load_json(NAMES_FILE, {})
        self._goals         = load_json(GOALS_FILE, {})
        self._tasks         = load_json(TASKS_FILE, {})
        self._space_numbers = load_json(NUMBERS_FILE, {})  # sid → n (int)
        self._deleted_sids  = set(load_json(DELETED_FILE, []))
        self._load_history_today()

        # Load pinned, migrating old single-id format → list format
        raw_pinned = load_json(PINNED_FILE, {})
        self._pinned = {}
        for sid, val in raw_pinned.items():
            if isinstance(val, list):
                self._pinned[sid] = val
            elif val is not None:
                self._pinned[sid] = [val]
            else:
                self._pinned[sid] = []

        # Numerar todos los espacios en orden visual antes de procesar el actual
        self._init_space_numbers()

        # ── Space inicial ──
        self._current_space = get_current_space_id()
        self._last_space_check = self._current_space
        sid = str(self._current_space)
        # Si el espacio actual estaba eliminado, lo restauramos
        self._deleted_sids.discard(sid)
        if sid not in self._space_seconds:
            self._space_seconds[sid] = 0.0
        if sid not in self._names:
            self._names[sid] = self._next_space_name(sid)
            save_json(NAMES_FILE, self._names)

        # ── Construir menú del Timer (rumps) ──
        self._build_menu()

        # ── Construir status item de Tareas (NSStatusItem nativo) ──
        self._ns_delegate_refs = []  # keep strong refs to prevent GC
        self._build_tasks_status_item()

        # ── Registrar observer de Spaces y wake ──
        self._observer = SpaceObserver.alloc().init()
        self._observer.app_ref = self
        ws = NSWorkspace.sharedWorkspace()
        nc = ws.notificationCenter()
        nc.addObserver_selector_name_object_(
            self._observer, "spaceDidChange:",
            "NSWorkspaceActiveSpaceDidChangeNotification", None,
        )
        nc.addObserver_selector_name_object_(
            self._observer, "systemDidWake_",
            "NSWorkspaceDidWakeNotification", None,
        )

        # ── Timers y threads ──
        self._main_timer = rumps.Timer(self._tick, TICK_INTERVAL)
        self._main_timer.start()
        self._start_autosave()
        self._start_idle_watcher()

        # ── SIGTERM para guardar antes de salir ──
        signal.signal(signal.SIGTERM, self._on_sigterm)

        log.info(f"DeskTimer iniciado. Space actual: {self._current_space}")

    # ── Nombres secuenciales ──────────────────────────────────────────────────

    def _init_space_numbers(self):
        """Asigna numeros secuenciales a todos los espacios en su orden visual real."""
        ordered = _get_ordered_space_ids()
        if not ordered:
            return
        changed = False
        for space_id in ordered:
            sid = str(space_id)
            if sid not in self._deleted_sids and sid not in self._space_numbers:
                n = max(self._space_numbers.values(), default=0) + 1
                self._space_numbers[sid] = n
                changed = True
        if changed:
            save_json(NUMBERS_FILE, self._space_numbers)

    def _next_space_name(self, sid):
        """Devuelve 'Escritorio N' asignando un numero secuencial al sid."""
        if sid not in self._space_numbers:
            n = max(self._space_numbers.values(), default=0) + 1
            self._space_numbers[sid] = n
            save_json(NUMBERS_FILE, self._space_numbers)
        return f"Escritorio {self._space_numbers[sid]}"

    # ── Carga de historial ─────────────────────────────────────────────────────

    def _load_history_today(self):
        history = load_json(HISTORY_FILE, {})
        today_str = str(self._today if hasattr(self, "_today") else datetime.date.today())
        self._space_seconds = {
            k: float(v)
            for k, v in history.get(today_str, {}).items()
        }

    # ── Construcción del menú Timer (rumps) ───────────────────────────────────

    def _build_menu(self):
        self.menu.clear()

        sid = str(self._current_space)
        name = self._names.get(sid, f"Espacio {sid}")

        # ── Info de espacio actual (no callback, tiene tooltip) ──
        self._mi_space_info = rumps.MenuItem(f"{name}  {fmt_time(0)}")
        self._mi_space_info.set_callback(None)
        self.menu.add(self._mi_space_info)

        # ── Resumen de tareas (no callback, tiene tooltip) ──
        self._mi_task_summary = rumps.MenuItem("Tasks: 0 pending")
        self._mi_task_summary.set_callback(None)
        self.menu.add(self._mi_task_summary)

        self.menu.add(rumps.separator)

        # ── Espacio actual (solo el activo, compacto) ──
        self._space_menu_items = {}
        sid = str(self._current_space)
        item = self._make_space_menu_item(sid, self._space_seconds.get(sid, 0.0))
        self._space_menu_items[sid] = item
        self.menu.add(item)

        self.menu.add(rumps.separator)

        # ── Sección Pomodoro ──
        self._mi_pomo_header = rumps.MenuItem("— Pomodoro —")
        self._mi_pomo_header.set_callback(None)
        self.menu.add(self._mi_pomo_header)

        self._mi_pomo_status = rumps.MenuItem("Enfoque: 25:00")
        self._mi_pomo_status.set_callback(None)
        self.menu.add(self._mi_pomo_status)

        self._mi_pomo_toggle = rumps.MenuItem("Iniciar", callback=self.toggle_pomo)
        self.menu.add(self._mi_pomo_toggle)

        self._mi_pomo_skip = rumps.MenuItem("Saltar fase", callback=self.skip_pomo_phase)
        self.menu.add(self._mi_pomo_skip)

        self._mi_pomo_sessions = rumps.MenuItem("Sesiones: 0")
        self._mi_pomo_sessions.set_callback(None)
        self.menu.add(self._mi_pomo_sessions)

        self.menu.add(rumps.separator)

        # ── Pausa global ──
        self._mi_pause = rumps.MenuItem("Pausar timer", callback=self.toggle_pause)
        self.menu.add(self._mi_pause)

        self.menu.add(rumps.separator)

        self.menu.add(rumps.MenuItem("Configuracion...", callback=self.open_settings))

        self.menu.add(rumps.separator)
        self.menu.add(rumps.MenuItem("Salir", callback=self._quit))

    def _make_space_menu_item(self, sid, secs):
        """Crea un MenuItem de espacio con submenu de acciones."""
        label = self._space_label(sid, secs)
        item = rumps.MenuItem(label)
        item.set_callback(None)

        rename_item = rumps.MenuItem("Renombrar...", callback=self._rename_space_action)
        rename_item._sid = sid
        goal_item = rumps.MenuItem("Meta diaria...", callback=self._goal_space_action)
        goal_item._sid = sid
        delete_item = rumps.MenuItem("Eliminar", callback=self._delete_space_action)
        delete_item._sid = sid

        item.update([rename_item, goal_item, rumps.separator, delete_item])
        return item

    def _space_label(self, sid, secs):
        name = self._names.get(sid, f"Espacio {sid}")
        time_str = fmt_time(secs)
        goal = self._goals.get(sid)
        marker = ">" if str(self._current_space) == sid else " "

        if goal and goal > 0:
            pct = min(secs / goal * 100, 100)
            goal_str = fmt_time(goal)
            if pct >= 100:
                goal_mark = "[meta]"
            elif pct >= 90:
                goal_mark = "[90%+]"
            elif pct >= 50:
                goal_mark = "[50%+]"
            else:
                goal_mark = "[<50%]"
            return f"{marker} {name:<14} {time_str} / {goal_str} {goal_mark}"
        return f"{marker} {name:<16} {time_str}"

    # ── Tasks status item (NSStatusItem nativo) ───────────────────────────────

    def _build_tasks_status_item(self):
        """Crea el segundo status item para tareas usando AppKit directamente."""
        self._tasks_si = NSStatusBar.systemStatusBar().statusItemWithLength_(
            NSVariableStatusItemLength
        )
        self._tasks_si.button().setTitle_("Tasks")
        self._tasks_nsmenu = NSMenu.alloc().init()
        self._tasks_nsmenu.setAutoenablesItems_(False)
        self._tasks_si.setMenu_(self._tasks_nsmenu)
        self._rebuild_tasks_ns_menu()

    def _rebuild_tasks_ns_menu(self):
        """Reconstruye completamente el menu de tareas (NSMenu nativo)."""
        # Clear all items
        while self._tasks_nsmenu.numberOfItems() > 0:
            self._tasks_nsmenu.removeItemAtIndex_(0)

        # Clear GC-prevention refs
        self._ns_delegate_refs = []

        sid = str(self._current_space)
        name = self._names.get(sid, f"Espacio {sid}")

        # ── Header (disabled) ──
        header_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            f"— Tareas: {name} —", None, ""
        )
        header_item.setEnabled_(False)
        self._tasks_nsmenu.addItem_(header_item)

        # ── Task rows ──
        pinned_list = self._pinned.get(sid, [])

        with self._task_lock:
            tasks = list(self._tasks.get(sid, []))

        tasks_sorted = self._sort_tasks(tasks, pinned_list)

        for task in tasks_sorted:
            tid = task["id"]
            pin_index = pinned_list.index(tid) if tid in pinned_list else None
            row_view = TaskRowView.rowForTask_app_pinIndex_(task, self, pin_index)

            mi = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("", None, "")
            mi.setEnabled_(True)
            mi.setView_(row_view)
            self._tasks_nsmenu.addItem_(mi)
            self._ns_delegate_refs.append(row_view)  # prevent GC

        # ── Add task view ──
        add_view = AddTaskView.viewForApp_(self)
        add_mi = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("", None, "")
        add_mi.setEnabled_(True)
        add_mi.setView_(add_view)
        self._tasks_nsmenu.addItem_(add_mi)
        self._ns_delegate_refs.append(add_view)
        if hasattr(add_view, "_delegate") and add_view._delegate is not None:
            self._ns_delegate_refs.append(add_view._delegate)

    def _sort_tasks(self, tasks, pinned_list):
        """Ordena: pinned en orden de lista, luego pendientes, luego done."""
        pinned_set = set(pinned_list)

        pinned_tasks = []
        for pid in pinned_list:
            task = next((t for t in tasks if t["id"] == pid), None)
            if task is not None:
                pinned_tasks.append(task)

        pending = [t for t in tasks if t["id"] not in pinned_set and not t.get("done", False)]
        done = [t for t in tasks if t["id"] not in pinned_set and t.get("done", False)]

        return pinned_tasks + pending + done

    def _update_tasks_si_title(self):
        """Actualiza el titulo del status item de tareas cada tick."""
        sid = str(self._current_space)
        pinned_list = self._pinned.get(sid, [])

        with self._task_lock:
            tasks = self._tasks.get(sid, [])
            # Find first non-done pinned task
            first_pinned_text = None
            for tid in pinned_list:
                task = next((t for t in tasks if t["id"] == tid), None)
                if task and not task.get("done", False):
                    first_pinned_text = task.get("text", "")
                    break

        if first_pinned_text:
            self._tasks_si.button().setTitle_(truncate(first_pinned_text, 22))
        else:
            self._tasks_si.button().setTitle_("Tasks")

    # ── Tooltips ──────────────────────────────────────────────────────────────

    def _update_hover_tooltips(self):
        """Actualiza los tooltips de _mi_space_info y _mi_task_summary."""
        sid = str(self._current_space)
        name = self._names.get(sid, f"Espacio {sid}")

        # Tooltip de espacio: tiempo actual + total de todos los espacios
        with self._lock:
            current_secs = self._get_live_seconds_unlocked(self._current_space)
            total_secs = sum(
                self._get_live_seconds_unlocked(int(s))
                if int(s) == self._current_space
                else self._space_seconds.get(s, 0.0)
                for s in self._space_seconds
            )
        space_tooltip = (
            f"{name}: {fmt_time(current_secs)}\n"
            f"Total hoy: {fmt_time(total_secs)}"
        )
        set_tooltip(self._mi_space_info, space_tooltip)

        # Resumen de tareas + tooltip
        pinned_list = self._pinned.get(sid, [])
        with self._task_lock:
            tasks = list(self._tasks.get(sid, []))

        pending_count = sum(1 for t in tasks if not t.get("done", False))
        self._mi_task_summary.title = f"Tasks: {pending_count} pending"

        if not tasks:
            task_tooltip = "(sin tareas)"
        else:
            lines = []
            pinned_set = set(pinned_list)
            for t in tasks:
                tid = t["id"]
                if tid in pinned_set:
                    idx = pinned_list.index(tid)
                    marker = str(idx + 1)  # 1-based
                elif t.get("done"):
                    marker = "✓"
                else:
                    marker = "○"
                lines.append(f"[{marker}] {t.get('text', '')}")
            task_tooltip = "\n".join(lines)

        set_tooltip(self._mi_task_summary, task_tooltip)

    # ── Tick principal (cada 1 segundo) ───────────────────────────────────────

    @rumps.timer(TICK_INTERVAL)
    def _tick(self, sender):
        # Cambio de dia
        today = datetime.date.today()
        with self._lock:
            if today != self._today:
                self._day_reset(today)

        # Fallback polling de Space
        current = get_current_space_id()
        if current != self._last_space_check:
            self._last_space_check = current
            self.handle_space_change()

        # Tick del Pomodoro
        self._pomo_tick()

        # Actualizar titulo de la barra de menu (item 1 - rumps)
        self._update_title()

        # Actualizar items de tiempo en el menu
        self._update_space_items()

        # Actualizar estado del Pomodoro en el menu
        self._update_pomo_items()

        # Actualizar info item de espacio actual
        self._update_space_info_item()

        # Actualizar titulo del tasks status item (item 2)
        self._update_tasks_si_title()

        # Actualizar tooltips
        self._update_hover_tooltips()

    def _update_title(self):
        with self._lock:
            if self._is_paused:
                sid = str(self._current_space)
                name = self._names.get(sid, f"Espacio {sid}")
                self.title = f"{name}  [pausado]"
                return
            sid = str(self._current_space)
            name = self._names.get(sid, f"Espacio {sid}")
            secs = self._get_live_seconds_unlocked(self._current_space)
            pomo_active = self._pomo_active and not self._pomo_paused

        pomo_suffix = "  [pomo]" if pomo_active else ""
        self.title = f"{name}  {fmt_time(secs)}{pomo_suffix}"

    def _update_space_info_item(self):
        sid = str(self._current_space)
        name = self._names.get(sid, f"Espacio {sid}")
        with self._lock:
            secs = self._get_live_seconds_unlocked(self._current_space)
        self._mi_space_info.title = f"{name}  {fmt_time(secs)}"

    def _update_space_items(self):
        # Solo actualiza el espacio actual (único visible)
        with self._lock:
            current = self._current_space
            live = self._get_live_seconds_unlocked(current)
        sid = str(current)
        item = self._space_menu_items.get(sid)
        if item:
            item._menuitem.setTitle_(self._space_label(sid, live))

    def _update_pomo_items(self):
        with self._pomo_lock:
            active    = self._pomo_active
            paused    = self._pomo_paused
            phase     = self._pomo_phase
            remaining = self._pomo_remaining
            sessions  = self._pomo_sessions

        phase_names = {"focus": "Enfoque", "break": "Descanso", "long_break": "Desc. largo"}
        name = phase_names.get(phase, phase)

        self._mi_pomo_status.title = f"{name}: {fmt_time(remaining)}"
        self._mi_pomo_sessions.title = f"Sesiones: {sessions}"

        if not active:
            self._mi_pomo_toggle.title = "Iniciar"
        elif paused:
            self._mi_pomo_toggle.title = "Reanudar"
        else:
            self._mi_pomo_toggle.title = "Pausar"

    # ── Calculo de tiempo en vivo ──────────────────────────────────────────────

    def _get_live_seconds_unlocked(self, space_id):
        """Debe llamarse con _lock adquirido."""
        sid = str(space_id)
        base = self._space_seconds.get(sid, 0.0)
        if space_id == self._current_space and not self._is_paused and not self._is_idle_paused:
            base += time.time() - self._session_start
        return base

    # ── Manejo de cambio de Space ──────────────────────────────────────────────

    def handle_space_change(self):
        new_id = get_current_space_id()
        sid = str(new_id)

        # Si el espacio fue eliminado, no lo re-anadimos al tracker
        if sid in self._deleted_sids:
            with self._lock:
                self._current_space    = new_id
                self._session_start    = time.time()
                self._last_space_check = new_id
            return

        with self._lock:
            if new_id == self._current_space:
                return

            # Acumular tiempo del Space anterior
            if not self._is_paused and not self._is_idle_paused:
                elapsed = time.time() - self._session_start
                old_sid = str(self._current_space)
                self._space_seconds[old_sid] = self._space_seconds.get(old_sid, 0.0) + elapsed

            self._current_space = new_id
            self._session_start = time.time()
            self._last_space_check = new_id

            if sid not in self._space_seconds:
                self._space_seconds[sid] = 0.0
            if sid not in self._names:
                self._names[sid] = self._next_space_name(sid)
                save_json(NAMES_FILE, self._names)

        # Reemplazar el ítem del espacio actual (solo mostramos el activo)
        new_sid = str(new_id)
        old_items = list(self._space_menu_items.items())
        for old_sid, old_item in old_items:
            self.menu._menu.removeItem_(old_item._menuitem)
        self._space_menu_items = {}
        new_item = self._make_space_menu_item(new_sid, self._space_seconds.get(new_sid, 0.0))
        self._space_menu_items[new_sid] = new_item
        # Insertar después de mi_task_summary (segundo ítem del menú)
        self.menu._menu.insertItem_atIndex_(new_item._menuitem,
            self.menu._menu.indexOfItem_(self._mi_task_summary._menuitem) + 1)

        # Reconstruir menu de tareas nativo
        self._rebuild_tasks_ns_menu()

        log.info(f"Space cambiado a {new_id} ({self._names.get(str(new_id), '?')})")

    # ── Manejo de sleep/wake ───────────────────────────────────────────────────

    def handle_wake(self):
        with self._lock:
            today = datetime.date.today()
            if today != self._today:
                self._day_reset(today)
            else:
                # Evitar acumular tiempo del sleep
                self._session_start = time.time()
        log.info("Sistema desperto de sleep")

    # ── Reset de dia ───────────────────────────────────────────────────────────

    def _day_reset(self, new_today):
        """Llamar con _lock adquirido."""
        self._save_history()
        self._space_seconds = {}
        self._session_start = time.time()
        self._today = new_today
        with self._pomo_lock:
            self._pomo_sessions = 0
        self._clean_old_done_tasks()
        log.info(f"Reset de dia: {new_today}")

    def _clean_old_done_tasks(self):
        today_str = str(datetime.date.today())
        with self._task_lock:
            for sid in self._tasks:
                self._tasks[sid] = [
                    t for t in self._tasks[sid]
                    if not t["done"] or t.get("date_done") == today_str
                ]
        save_json(TASKS_FILE, self._tasks)

    # ── Pausa global ───────────────────────────────────────────────────────────

    def toggle_pause(self, sender=None):
        with self._lock:
            if not self._is_paused:
                # Acumular tiempo antes de pausar
                elapsed = time.time() - self._session_start
                sid = str(self._current_space)
                self._space_seconds[sid] = self._space_seconds.get(sid, 0.0) + elapsed
                self._is_paused = True
                with self._pomo_lock:
                    self._pomo_was_active = self._pomo_active and not self._pomo_paused
                    if self._pomo_was_active:
                        self._pomo_paused = True
                self._mi_pause.title = "Reanudar timer"
            else:
                self._is_paused = False
                self._is_idle_paused = False
                self._session_start = time.time()
                with self._pomo_lock:
                    if self._pomo_was_active:
                        self._pomo_paused = False
                        self._pomo_last_tick = time.time()
                self._mi_pause.title = "Pausar timer"

    # ── Idle watcher ───────────────────────────────────────────────────────────

    def _start_idle_watcher(self):
        def watcher():
            while True:
                time.sleep(10)
                self._check_idle()
        t = threading.Thread(target=watcher, daemon=True, name="idle-watcher")
        t.start()

    def _check_idle(self):
        idle = get_idle_seconds()
        threshold = self._settings.get("idle_threshold", IDLE_THRESHOLD)
        resume_at  = self._settings.get("idle_resume", IDLE_RESUME_AT)

        with self._lock:
            if self._is_paused:
                return

            if not self._is_idle_paused and idle >= threshold:
                # Pausa por inactividad — descontar los segundos idle del Space
                elapsed = time.time() - self._session_start
                sid = str(self._current_space)
                accumulated = self._space_seconds.get(sid, 0.0) + elapsed - threshold
                self._space_seconds[sid] = max(0.0, accumulated)
                self._is_idle_paused = True
                log.info(f"Pausa automatica por idle ({idle:.0f}s)")
                return

            if self._is_idle_paused and idle < resume_at:
                self._is_idle_paused = False
                self._session_start = time.time()
                log.info("Reanudado tras idle")

    # ── Autosave ───────────────────────────────────────────────────────────────

    def _start_autosave(self):
        def saver():
            while True:
                time.sleep(AUTOSAVE_INTERVAL)
                self._save_all()
        t = threading.Thread(target=saver, daemon=True, name="autosave")
        t.start()

    def _save_all(self):
        self._save_history()
        save_json(NAMES_FILE, self._names)
        save_json(GOALS_FILE, self._goals)
        with self._task_lock:
            save_json(TASKS_FILE, self._tasks)
        save_json(SETTINGS_FILE, self._settings)
        save_json(PINNED_FILE, self._pinned)
        save_json(NUMBERS_FILE, self._space_numbers)
        save_json(DELETED_FILE, list(self._deleted_sids))

    def _save_history(self):
        history = load_json(HISTORY_FILE, {})
        today_str = str(self._today)
        with self._lock:
            snapshot = dict(self._space_seconds)
            current = self._current_space
            if not self._is_paused and not self._is_idle_paused:
                sid = str(current)
                snapshot[sid] = snapshot.get(sid, 0.0) + (time.time() - self._session_start)
        history[today_str] = {k: round(v, 1) for k, v in snapshot.items()}
        save_json(HISTORY_FILE, history)

    # ── Pomodoro ───────────────────────────────────────────────────────────────

    def _pomo_tick(self):
        with self._pomo_lock:
            if not self._pomo_active or self._pomo_paused:
                return
            with self._lock:
                if self._is_paused or self._is_idle_paused:
                    return

            now = time.time()
            if self._pomo_last_tick:
                self._pomo_remaining -= now - self._pomo_last_tick
            self._pomo_last_tick = now

            if self._pomo_remaining <= 0:
                self._complete_pomo_phase()

    def _complete_pomo_phase(self):
        """Llamar con _pomo_lock adquirido."""
        sessions_for_long = self._settings.get("pomo_sessions_for_long", 4)

        if self._pomo_phase == "focus":
            self._pomo_sessions += 1
            play_sound("Glass")
            rumps.notification(
                "DeskTimer", "Pomodoro",
                f"Sesion completada! Sesiones hoy: {self._pomo_sessions}",
                sound=False,
            )
            if self._pomo_sessions % sessions_for_long == 0:
                self._pomo_phase = "long_break"
                self._pomo_remaining = self._settings.get("pomo_long_break", 900)
                play_sound("Hero")
                rumps.notification("DeskTimer", "Pomodoro", "Descanso largo! 15 minutos", sound=False)
            else:
                self._pomo_phase = "break"
                self._pomo_remaining = self._settings.get("pomo_break", 300)
                play_sound("Purr")
                rumps.notification("DeskTimer", "Pomodoro", "Descanso corto! 5 minutos", sound=False)
        else:
            self._pomo_phase = "focus"
            self._pomo_remaining = self._settings.get("pomo_focus", 1500)
            play_sound("Glass")
            rumps.notification("DeskTimer", "Pomodoro", "Tiempo de enfoque!", sound=False)

        self._pomo_last_tick = time.time()

    def toggle_pomo(self, sender):
        with self._pomo_lock:
            if not self._pomo_active:
                self._pomo_active = True
                self._pomo_paused = False
                self._pomo_last_tick = time.time()
                if self._pomo_remaining <= 0:
                    self._pomo_phase = "focus"
                    self._pomo_remaining = self._settings.get("pomo_focus", 1500)
            elif not self._pomo_paused:
                self._pomo_paused = True
            else:
                self._pomo_paused = False
                self._pomo_last_tick = time.time()

    def skip_pomo_phase(self, sender):
        with self._pomo_lock:
            self._complete_pomo_phase()

    # ── Tareas: acciones de TaskRowView ───────────────────────────────────────

    def toggle_task_done(self, task_id):
        sid = str(self._current_space)
        pinned_list = self._pinned.get(sid, [])
        became_done = False

        with self._task_lock:
            tasks = self._tasks.get(sid, [])
            for task in tasks:
                if task["id"] == task_id:
                    task["done"] = not task["done"]
                    task["date_done"] = str(datetime.date.today()) if task["done"] else None
                    became_done = task["done"]
                    break
            save_json(TASKS_FILE, self._tasks)

        # Si se marco como done y estaba en la lista de pinned, removerlo
        if became_done and task_id in pinned_list:
            pinned_list.remove(task_id)
            self._pinned[sid] = pinned_list
            save_json(PINNED_FILE, self._pinned)

        self._rebuild_tasks_ns_menu()

    def toggle_task_pin(self, task_id):
        sid = str(self._current_space)
        pinned_list = self._pinned.get(sid, [])

        if task_id in pinned_list:
            # Despinear
            pinned_list.remove(task_id)
            with self._task_lock:
                tasks = self._tasks.get(sid, [])
                for task in tasks:
                    if task["id"] == task_id:
                        task["pinned"] = False
                        break
        else:
            # Pinear: agregar al final
            pinned_list.append(task_id)
            with self._task_lock:
                tasks = self._tasks.get(sid, [])
                for task in tasks:
                    if task["id"] == task_id:
                        task["pinned"] = True
                        break

        self._pinned[sid] = pinned_list
        save_json(PINNED_FILE, self._pinned)
        save_json(TASKS_FILE, self._tasks)
        self._rebuild_tasks_ns_menu()

    def rename_task(self, task_id, new_text):
        sid = str(self._current_space)
        with self._task_lock:
            tasks = self._tasks.get(sid, [])
            for task in tasks:
                if task["id"] == task_id:
                    task["text"] = new_text
                    break
            save_json(TASKS_FILE, self._tasks)

    def delete_task(self, task_id):
        sid = str(self._current_space)
        with self._task_lock:
            tasks = self._tasks.get(sid, [])
            self._tasks[sid] = [t for t in tasks if t["id"] != task_id]
            save_json(TASKS_FILE, self._tasks)

        # Limpiar pin si corresponde
        pinned_list = self._pinned.get(sid, [])
        if task_id in pinned_list:
            pinned_list.remove(task_id)
            self._pinned[sid] = pinned_list
            save_json(PINNED_FILE, self._pinned)

        self._rebuild_tasks_ns_menu()

    def add_task_from_text(self, text):
        """Agregar tarea desde el campo inline y re-enfocar el input."""
        sid = str(self._current_space)
        new_task = {
            "id": uuid.uuid4().hex[:6],
            "text": text,
            "done": False,
            "date_done": None,
            "pinned": False,
        }
        with self._task_lock:
            if sid not in self._tasks:
                self._tasks[sid] = []
            self._tasks[sid].append(new_task)
            save_json(TASKS_FILE, self._tasks)
        self._rebuild_tasks_ns_menu()
        self._focus_add_field()

    def _focus_add_field(self):
        """Pone el foco en el campo de texto del AddTaskView tras reconstruir el menú."""
        try:
            from AppKit import NSApp
            n = self._tasks_nsmenu.numberOfItems()
            if n == 0:
                return
            last_view = self._tasks_nsmenu.itemAtIndex_(n - 1).view()
            if last_view and hasattr(last_view, "_text_field"):
                win = NSApp.keyWindow()
                if win:
                    win.makeFirstResponder_(last_view._text_field)
        except Exception as e:
            log.debug(f"_focus_add_field: {e}")

    # ── Gestion de Spaces: acciones del submenu ────────────────────────────────

    def _rename_space_action(self, sender):
        sid = getattr(sender, "_sid", None)
        if not sid:
            return
        current_name = self._names.get(sid, f"Espacio {sid}")
        win = rumps.Window(
            message=f"Nuevo nombre para Space ID {sid}:",
            title="DeskTimer — Renombrar",
            default_text=current_name,
            ok="Guardar",
            cancel="Cancelar",
            dimensions=(300, 24),
        )
        response = win.run()
        if response.clicked and response.text.strip():
            self._names[sid] = response.text.strip()
            save_json(NAMES_FILE, self._names)
            # Rebuild tasks menu to reflect new name
            self._rebuild_tasks_ns_menu()
            # Update space item label
            if sid in self._space_menu_items:
                with self._lock:
                    secs = self._get_live_seconds_unlocked(int(sid))
                self._space_menu_items[sid].title = self._space_label(sid, secs)

    def _goal_space_action(self, sender):
        sid = getattr(sender, "_sid", None)
        if not sid:
            return
        name = self._names.get(sid, f"Espacio {sid}")
        current_goal = self._goals.get(sid, 0)
        current_hours = current_goal / 3600 if current_goal else ""
        win = rumps.Window(
            message=f"Meta diaria para '{name}' (en horas, 0 = sin meta):",
            title="DeskTimer — Meta diaria",
            default_text=str(current_hours),
            ok="Guardar",
            cancel="Cancelar",
            dimensions=(300, 24),
        )
        response = win.run()
        if response.clicked:
            try:
                hours = float(response.text.strip())
                if hours > 0:
                    self._goals[sid] = int(hours * 3600)
                else:
                    self._goals.pop(sid, None)
                save_json(GOALS_FILE, self._goals)
            except ValueError:
                pass

    def _delete_space_action(self, sender):
        sid = getattr(sender, "_sid", None)
        if not sid:
            return

        # No eliminar el espacio actualmente activo
        if int(sid) == self._current_space:
            return

        # Eliminar de todas las estructuras de datos
        with self._lock:
            self._space_seconds.pop(sid, None)
        self._names.pop(sid, None)
        self._goals.pop(sid, None)
        with self._task_lock:
            self._tasks.pop(sid, None)
        self._pinned.pop(sid, None)

        # Marcar como eliminado para que no reaparezca al cambiar a el
        self._deleted_sids.add(sid)
        self._space_numbers.pop(sid, None)

        # Eliminar del historial de hoy
        history = load_json(HISTORY_FILE, {})
        today_str = str(self._today)
        if today_str in history:
            history[today_str].pop(sid, None)
            save_json(HISTORY_FILE, history)

        save_json(NAMES_FILE, self._names)
        save_json(GOALS_FILE, self._goals)
        save_json(TASKS_FILE, self._tasks)
        save_json(PINNED_FILE, self._pinned)
        save_json(NUMBERS_FILE, self._space_numbers)
        save_json(DELETED_FILE, list(self._deleted_sids))

        # Eliminar del menu usando NSMenu directamente
        if sid in self._space_menu_items:
            item = self._space_menu_items.pop(sid)
            self.menu._menu.removeItem_(item._menuitem)

        log.info(f"Espacio {sid} eliminado")

    # ── Configuracion ──────────────────────────────────────────────────────────

    def open_settings(self, sender):
        fields = [
            ("pomo_focus",             "Duracion enfoque Pomodoro (minutos)"),
            ("pomo_break",             "Duracion descanso corto (minutos)"),
            ("pomo_long_break",        "Duracion descanso largo (minutos)"),
            ("pomo_sessions_for_long", "Sesiones hasta descanso largo"),
            ("idle_threshold",         "Umbral de inactividad para pausa (segundos)"),
            ("idle_resume",            "Umbral de reanudacion tras inactividad (segundos)"),
        ]
        pomo_keys = {"pomo_focus", "pomo_break", "pomo_long_break"}

        for key, label in fields:
            current = self._settings.get(key, DEFAULT_SETTINGS.get(key, 0))
            display = current // 60 if key in pomo_keys else current
            win = rumps.Window(
                message=f"{label}:",
                title="DeskTimer — Configuracion",
                default_text=str(display),
                ok="Guardar",
                cancel="Cancelar",
                dimensions=(300, 24),
            )
            response = win.run()
            if response.clicked:
                try:
                    val = int(response.text.strip())
                    if val > 0:
                        self._settings[key] = val * 60 if key in pomo_keys else val
                except ValueError:
                    pass

        save_json(SETTINGS_FILE, self._settings)

    # ── Salir ──────────────────────────────────────────────────────────────────

    def _quit(self, sender=None):
        self._save_all()
        rumps.quit_application()

    def _on_sigterm(self, signum, frame):
        self._save_all()

# ─── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    app = DeskTimerApp()
    app.run()
