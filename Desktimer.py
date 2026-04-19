# -*- coding: utf-8 -*-
"""
DeskTimer para macOS
Tracker pasivo de tiempo por Space (escritorio virtual) de macOS.
Incluye: Timer por Space, Pomodoro integrado, To-Do list por Space, Metas diarias.

Dependencias:
    pip install rumps pyobjc-framework-Cocoa pyobjc-framework-Quartz

Uso:
    python desktimer.py
"""

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
import subprocess

from AppKit import NSWorkspace, NSObject, NSSound
import Quartz

# ─────────────────────────────────────────────
# Constantes
# ─────────────────────────────────────────────

DIRECTORIO_DATOS = os.path.expanduser("~/.desktimer")
HISTORIAL_PATH = os.path.join(DIRECTORIO_DATOS, "space_history.json")
NOMBRES_PATH = os.path.join(DIRECTORIO_DATOS, "space_names.json")
METAS_PATH = os.path.join(DIRECTORIO_DATOS, "space_goals.json")
TAREAS_PATH = os.path.join(DIRECTORIO_DATOS, "space_tasks.json")
SETTINGS_PATH = os.path.join(DIRECTORIO_DATOS, "settings.json")

UMBRAL_INACTIVIDAD = 200  # segundos sin input → pausa automática
REANUDAR_INACTIVIDAD = 30  # segundos de actividad → reanudar
INTERVALO_TICK = 1  # segundos entre ticks del timer principal
INTERVALO_AUTOSAVE = 30  # segundos entre guardados automáticos
INTERVALO_IDLE = 10  # segundos entre verificaciones de inactividad

SETTINGS_DEFAULT = {
    "pomo_focus": 1500,  # 25 minutos
    "pomo_break": 300,  # 5 minutos
    "pomo_long_break": 900,  # 15 minutos
    "pomo_sessions_for_long": 4,
    "idle_threshold": UMBRAL_INACTIVIDAD,
    "idle_resume": REANUDAR_INACTIVIDAD,
}

# ─────────────────────────────────────────────
# API privada de CoreGraphics para Spaces
# ─────────────────────────────────────────────


def _cargar_cg():
    """Carga CoreGraphics y prepara las funciones privadas de Spaces."""
    try:
        lib = ctypes.util.find_library("CoreGraphics")
        cg = ctypes.cdll.LoadLibrary(lib)

        # El nombre exacto de la funcion varia segun la version de macOS.
        # Probamos los dos nombres conocidos.
        _conexion_defecto = None
        for nombre in ("CGSGetDefaultConnectionForProcess", "_CGSDefaultConnection"):
            try:
                fn = getattr(cg, nombre)
                fn.restype = ctypes.c_int
                # Verificar que funciona llamandola
                _ = fn()
                _conexion_defecto = fn
                break
            except Exception:
                continue

        if _conexion_defecto is None:
            # Ultimo recurso: usar CGMainDisplayID como proxy
            # (no da la conexion real, pero permite continuar con polling)
            print(
                "[desktimer] Advertencia: CGSGetDefaultConnectionForProcess no encontrada. "
                "Usando modo polling para detectar Spaces."
            )
            return None, None

        _get_space = cg.CGSGetActiveSpace
        _get_space.argtypes = [ctypes.c_int]
        _get_space.restype = ctypes.c_uint64

        return _conexion_defecto, _get_space
    except Exception as e:
        print(f"[desktimer] No se pudo cargar CoreGraphics: {e}")
        return None, None


_cg_conexion, _cg_get_space = _cargar_cg()


def obtener_space_actual():
    """Retorna el ID del Space activo (int). Retorna -1 si falla."""
    if _cg_conexion is None or _cg_get_space is None:
        return -1
    try:
        conn = _cg_conexion()
        return int(_cg_get_space(conn))
    except Exception:
        return -1


# ─────────────────────────────────────────────
# Helpers: tiempo, sonido, notificaciones
# ─────────────────────────────────────────────


def fmt_tiempo(segundos):
    """Formatea segundos como H:MM:SS."""
    s = max(0, int(segundos))
    h = s // 3600
    m = (s % 3600) // 60
    seg = s % 60
    return f"{h}:{m:02d}:{seg:02d}"


def fmt_pomo(segundos):
    """Formatea segundos del Pomodoro como MM:SS."""
    s = max(0, int(segundos))
    m = s // 60
    seg = s % 60
    return f"{m:02d}:{seg:02d}"


def get_idle_seconds():
    """Retorna los segundos de inactividad del usuario."""
    try:
        return Quartz.CGEventSourceSecondsSinceLastEventType(
            Quartz.kCGEventSourceStateCombinedSessionState, Quartz.kCGAnyInputEventType
        )
    except Exception:
        return 0


def reproducir_sonido(nombre_sistema="Glass"):
    """Reproduce un sonido del sistema usando NSSound."""
    try:
        sonido = NSSound.soundNamed_(nombre_sistema)
        if sonido:
            sonido.play()
    except Exception:
        # Fallback: afplay con sonido del sistema
        try:
            subprocess.Popen(
                ["afplay", f"/System/Library/Sounds/{nombre_sistema}.aiff"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception:
            pass


def enviar_notificacion(titulo, mensaje, sonido=False):
    """Envía una notificación nativa de macOS."""
    try:
        rumps.notification(
            title="DeskTimer", subtitle=titulo, message=mensaje, sound=sonido
        )
    except Exception:
        pass


# ─────────────────────────────────────────────
# Observer de cambios de Space (NSWorkspace)
# ─────────────────────────────────────────────


class ObservadorSpaces(NSObject):
    """Subclase de NSObject que recibe notificaciones de cambio de Space."""

    app_ref = None  # Referencia a DeskTimerApp (se asigna antes de registrar)

    def spaceDidChange_(self, notification):
        if self.app_ref:
            self.app_ref.manejar_cambio_space()

    # IMPORTANTE: los selectores de Objective-C solo admiten ASCII.
    # Usamos "sistemaDespierto_" (sin tilde) en lugar de "sistemaDespertó_".
    def sistemaDespierto_(self, notification):
        if self.app_ref:
            self.app_ref.manejar_despertar()


# ─────────────────────────────────────────────
# Persistencia
# ─────────────────────────────────────────────


def _cargar_json(path, default):
    """Carga un archivo JSON. Si falla, retorna el default."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _guardar_json(path, datos):
    """Guarda datos en un archivo JSON de forma segura."""
    try:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(datos, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception as e:
        print(f"[desktimer] Error al guardar {path}: {e}")


# ─────────────────────────────────────────────
# App principal
# ─────────────────────────────────────────────


class DeskTimerApp(rumps.App):

    def __init__(self):
        super().__init__("⏱ Cargando...", quit_button=None)

        # Asegurar directorio de datos
        os.makedirs(DIRECTORIO_DATOS, exist_ok=True)

        # ── Estado del timer ──
        self._lock = threading.Lock()
        self._current_space = None  # Space ID actual (int)
        self._space_seconds = {}  # {str(space_id): float}
        self._session_start = time.time()
        self._is_paused = False  # pausa global manual
        self._is_idle_paused = False  # pausa automática por inactividad
        self._idle_paused_at = None  # timestamp de cuándo se activó la pausa idle
        self._today = datetime.date.today()

        # ── Estado Pomodoro ──
        self._pomo_lock = threading.Lock()
        self._pomo_activo = False
        self._pomo_pausado = False
        self._pomo_fase = "focus"
        self._pomo_restante = 1500
        self._pomo_ultimo_tick = None
        self._pomo_sesiones = 0
        self._pomo_sesiones_fecha = str(self._today)
        self._pomo_estaba_activo = False  # para restaurar tras pausa global

        # ── Estado tareas ──
        self._task_lock = threading.Lock()
        self._tareas = {}  # {str(space_id): [lista de tareas]}

        # ── Cargar todos los datos ──
        self._cargar_todo()

        # ── Detectar Space inicial ──
        space_id = obtener_space_actual()
        with self._lock:
            self._current_space = space_id
            sid = str(space_id)
            if sid not in self._space_seconds:
                self._space_seconds[sid] = 0.0
        self._asegurar_nombre_space(space_id)

        # ── Registrar observer de Spaces ──
        self._observer = self._registrar_observer()

        # ── Construir menú inicial ──
        self._construir_menu()

        # ── Timer principal (1 segundo) ──
        self._timer_principal = rumps.Timer(self._tick, INTERVALO_TICK)
        self._timer_principal.start()

        # ── Threads daemon ──
        self._iniciar_autosave()
        self._iniciar_idle_watcher()

        # ── Manejo de señales para guardar al cerrar ──
        signal.signal(signal.SIGTERM, self._guardar_y_salir)
        signal.signal(signal.SIGINT, self._guardar_y_salir)

    # ─────────────────────────────────────────
    # Carga y guardado de datos
    # ─────────────────────────────────────────

    def _cargar_todo(self):
        """Carga todos los archivos JSON."""
        # Historial
        historial_completo = _cargar_json(HISTORIAL_PATH, {})
        hoy_str = str(self._today)
        with self._lock:
            self._space_seconds = {
                k: float(v) for k, v in historial_completo.get(hoy_str, {}).items()
            }

        # Nombres, metas, settings
        self._nombres = _cargar_json(NOMBRES_PATH, {})
        self._metas = _cargar_json(METAS_PATH, {})
        self._settings = {**SETTINGS_DEFAULT, **_cargar_json(SETTINGS_PATH, {})}

        # Tareas (limpiar completadas de días anteriores)
        tareas_raw = _cargar_json(TAREAS_PATH, {})
        with self._task_lock:
            self._tareas = tareas_raw
        self._limpiar_tareas_antiguas()

        # Sesiones Pomodoro (reset si el día cambió)
        sesiones_info = self._settings.get("pomo_sesiones_hoy", {})
        if sesiones_info.get("fecha") == str(self._today):
            self._pomo_sesiones = sesiones_info.get("count", 0)
        else:
            self._pomo_sesiones = 0

    def _guardar_historial(self):
        """Guarda el historial de hoy en space_history.json."""
        hoy_str = str(self._today)
        historial = _cargar_json(HISTORIAL_PATH, {})
        with self._lock:
            segundos_snapshot = dict(self._space_seconds)
        historial[hoy_str] = segundos_snapshot
        _guardar_json(HISTORIAL_PATH, historial)

    def _guardar_todo(self):
        """Guarda todos los archivos JSON."""
        self._guardar_historial()
        _guardar_json(NOMBRES_PATH, self._nombres)
        _guardar_json(METAS_PATH, self._metas)
        _guardar_json(
            SETTINGS_PATH,
            {
                **self._settings,
                "pomo_sesiones_hoy": {
                    "fecha": str(self._today),
                    "count": self._pomo_sesiones,
                },
            },
        )
        with self._task_lock:
            _guardar_json(TAREAS_PATH, self._tareas)

    def _limpiar_tareas_antiguas(self):
        """Elimina tareas completadas de días anteriores."""
        hoy_str = str(self._today)
        with self._task_lock:
            for sid in self._tareas:
                self._tareas[sid] = [
                    t
                    for t in self._tareas[sid]
                    if not t.get("done") or t.get("date_done") == hoy_str
                ]

    # ─────────────────────────────────────────
    # Gestión de Spaces y nombres
    # ─────────────────────────────────────────

    def _asegurar_nombre_space(self, space_id):
        """Asigna un nombre por defecto si el Space no tiene nombre."""
        sid = str(space_id)
        if sid not in self._nombres:
            n = len(self._nombres) + 1
            self._nombres[sid] = f"Espacio {n}"

    def nombre_space(self, space_id):
        """Retorna el nombre del Space (o 'Espacio N' si no tiene)."""
        sid = str(space_id)
        if sid not in self._nombres:
            self._asegurar_nombre_space(space_id)
        return self._nombres[sid]

    def _segundos_vivos(self, space_id):
        """Retorna el tiempo acumulado + sesión activa sin modificar el estado."""
        sid = str(space_id)
        with self._lock:
            base = self._space_seconds.get(sid, 0.0)
            activo = (
                space_id == self._current_space
                and not self._is_paused
                and not self._is_idle_paused
            )
            if activo:
                base += time.time() - self._session_start
        return base

    def _spaces_conocidos(self):
        """Retorna lista de space_ids conocidos (ordenados por nombre)."""
        with self._lock:
            ids_en_historial = set(self._space_seconds.keys())
        ids_con_nombre = set(self._nombres.keys())
        todos = ids_en_historial | ids_con_nombre
        # Ordenar: primero el actual, luego el resto por nombre
        with self._lock:
            actual_sid = str(self._current_space)

        def _orden(sid):
            if sid == actual_sid:
                return (0, self._nombres.get(sid, sid))
            return (1, self._nombres.get(sid, sid))

        return sorted(todos, key=_orden)

    # ─────────────────────────────────────────
    # Manejo de cambio de Space
    # ─────────────────────────────────────────

    def manejar_cambio_space(self):
        """Llamado cuando el usuario cambia de Space."""
        nuevo_id = obtener_space_actual()
        with self._lock:
            if nuevo_id == self._current_space:
                return  # misma notificación duplicada (fullscreen apps, etc.)

            # Acumular tiempo del Space anterior
            if not self._is_paused and not self._is_idle_paused:
                elapsed = time.time() - self._session_start
                sid_anterior = str(self._current_space)
                self._space_seconds[sid_anterior] = (
                    self._space_seconds.get(sid_anterior, 0.0) + elapsed
                )

            # Cambiar al nuevo Space
            self._current_space = nuevo_id
            self._session_start = time.time()
            sid_nuevo = str(nuevo_id)
            if sid_nuevo not in self._space_seconds:
                self._space_seconds[sid_nuevo] = 0.0

        self._asegurar_nombre_space(nuevo_id)
        # Reconstruir sección de tareas y marcar Space actual en menú
        self._actualizar_seccion_tareas()
        self._actualizar_seccion_espacios()

    def manejar_despertar(self):
        """Llamado cuando la Mac despierta de sleep."""
        hoy = datetime.date.today()
        with self._lock:
            # Si saltamos de día durante el sleep
            if hoy != self._today:
                self._resetear_dia(hoy)
            else:
                # Resetear session_start para no contar el tiempo de sleep
                self._session_start = time.time()

    # ─────────────────────────────────────────
    # Registrar observer de Spaces con NSWorkspace
    # ─────────────────────────────────────────

    def _registrar_observer(self):
        """Registra el ObservadorSpaces con NSWorkspace."""
        try:
            ObservadorSpaces.app_ref = self
            observer = ObservadorSpaces.alloc().init()

            workspace = NSWorkspace.sharedWorkspace()
            nc = workspace.notificationCenter()

            nc.addObserver_selector_name_object_(
                observer,
                "spaceDidChange:",
                "NSWorkspaceActiveSpaceDidChangeNotification",
                None,
            )
            nc.addObserver_selector_name_object_(
                observer, "sistemaDespierto:", "NSWorkspaceDidWakeNotification", None
            )
            return observer
        except Exception as e:
            print(f"[desktimer] Error registrando observer: {e}")
            return None

    # ─────────────────────────────────────────
    # Construcción del menú con rumps
    # ─────────────────────────────────────────

    def _construir_menu(self):
        """Construye el menú completo. Solo se llama una vez al inicio."""
        self.menu.clear()

        # ── Sección: Espacios ──
        self._item_header_espacios = rumps.MenuItem("── Espacios ──", callback=None)
        self._item_header_espacios.set_callback(None)
        self.menu.add(self._item_header_espacios)

        self._items_espacios = {}  # sid → MenuItem
        for sid in self._spaces_conocidos():
            item = rumps.MenuItem("", callback=None)
            self._items_espacios[sid] = item
            self.menu.add(item)

        self.menu.add(rumps.separator)

        # ── Sección: Pomodoro ──
        self._item_pomo_estado = rumps.MenuItem("🍅 Pomodoro inactivo", callback=None)
        self._item_pomo_accion = rumps.MenuItem("▶ Iniciar", callback=self._toggle_pomo)
        self._item_pomo_saltar = rumps.MenuItem(
            "⏭ Saltar fase", callback=self._saltar_fase_pomo
        )
        self._item_pomo_sesiones = rumps.MenuItem("Sesiones hoy: 0", callback=None)

        self.menu.add(rumps.MenuItem("── Pomodoro ──", callback=None))
        self.menu.add(self._item_pomo_estado)
        self.menu.add(self._item_pomo_accion)
        self.menu.add(self._item_pomo_saltar)
        self.menu.add(self._item_pomo_sesiones)

        self.menu.add(rumps.separator)

        # ── Sección: Tareas ──
        with self._lock:
            space_actual = self._current_space
        nombre_actual = self.nombre_space(space_actual)
        self._item_header_tareas = rumps.MenuItem(
            f"── Tareas ({nombre_actual}) ──", callback=None
        )
        self.menu.add(self._item_header_tareas)

        self._items_tareas = {}  # id_tarea → MenuItem
        self._item_agregar_tarea = rumps.MenuItem(
            "+ Agregar tarea...", callback=self._agregar_tarea
        )
        self.menu.add(self._item_agregar_tarea)

        self.menu.add(rumps.separator)

        # ── Controles globales ──
        self._item_pausa = rumps.MenuItem("⏸ Pausar timer", callback=self._toggle_pausa)
        self.menu.add(self._item_pausa)

        self.menu.add(rumps.separator)

        # ── Preferencias ──
        self.menu.add(
            rumps.MenuItem("Renombrar Spaces...", callback=self._renombrar_spaces)
        )
        self.menu.add(
            rumps.MenuItem("Configurar metas...", callback=self._configurar_metas)
        )
        self.menu.add(
            rumps.MenuItem("Configuración...", callback=self._abrir_configuracion)
        )

        self.menu.add(rumps.separator)

        # ── Ver historial por día ──
        self.menu.add(rumps.MenuItem("Ver historial...", callback=self._ver_historial))

        self.menu.add(rumps.separator)
        self.menu.add(rumps.MenuItem("Salir", callback=self._salir))

        # Render inicial
        self._actualizar_seccion_espacios()
        self._actualizar_seccion_tareas()
        self._actualizar_seccion_pomo()

    # ─────────────────────────────────────────
    # Actualización de secciones del menú
    # ─────────────────────────────────────────

    def _actualizar_titulo_barra(self):
        """Actualiza el título siempre visible en la barra de menú."""
        with self._lock:
            pausado = self._is_paused
            idle_pausado = self._is_idle_paused
            space_id = self._current_space
        with self._pomo_lock:
            pomo_activo = self._pomo_activo

        if pausado:
            self.title = "⏱ ⏸ Pausado"
        elif idle_pausado:
            self.title = "⏱ 💤 Inactivo"
        else:
            secs = self._segundos_vivos(space_id)
            nombre = self.nombre_space(space_id)
            pomo_icono = "🍅 " if pomo_activo else ""
            self.title = f"⏱ {pomo_icono}{nombre} {fmt_tiempo(secs)}"

    def _actualizar_seccion_espacios(self):
        """Actualiza los items de tiempo por Space en el menú."""
        with self._lock:
            actual_sid = str(self._current_space)

        spaces = self._spaces_conocidos()

        # Añadir items nuevos si aparecieron Spaces nuevos
        for sid in spaces:
            if sid not in self._items_espacios:
                item = rumps.MenuItem("", callback=None)
                self._items_espacios[sid] = item
                # Insertar antes del primer separator (posición variable)
                # Lo añadimos al menú después del header de espacios
                try:
                    self.menu.insert_after("── Espacios ──", item)
                except Exception:
                    self.menu.add(item)

        # Actualizar títulos
        for sid in spaces:
            item = self._items_espacios.get(sid)
            if not item:
                continue
            space_id_int = int(sid)
            secs = self._segundos_vivos(space_id_int)
            nombre = self.nombre_space(space_id_int)
            meta = self._metas.get(sid)

            # Indicador de meta
            if meta:
                pct = secs / meta * 100
                if pct >= 100:
                    indicador = "✅"
                elif pct >= 90:
                    indicador = "🟢"
                elif pct >= 50:
                    indicador = "🟡"
                else:
                    indicador = "🔴"
                meta_str = f" / {fmt_tiempo(meta)} ({int(pct)}%)"
            else:
                indicador = ""
                meta_str = ""

            # Marcar Space actual con ●
            marcador = "● " if sid == actual_sid else "  "
            item.title = (
                f"{marcador}{indicador}{nombre:<16} {fmt_tiempo(secs)}{meta_str}"
            )

    def _actualizar_seccion_pomo(self):
        """Actualiza los items de Pomodoro en el menú."""
        with self._pomo_lock:
            activo = self._pomo_activo
            pausado = self._pomo_pausado
            fase = self._pomo_fase
            restante = self._pomo_restante
            sesiones = self._pomo_sesiones

        if not activo:
            self._item_pomo_estado.title = "🍅 Pomodoro inactivo"
            self._item_pomo_accion.title = "▶ Iniciar"
        else:
            if fase == "focus":
                icono = "🍅"
                label = "Enfoque"
            elif fase == "break":
                icono = "☕"
                label = "Descanso"
            else:
                icono = "🌴"
                label = "Descanso largo"

            self._item_pomo_estado.title = f"{icono} {label}: {fmt_pomo(restante)}"

            if pausado:
                self._item_pomo_accion.title = "▶ Reanudar Pomodoro"
            else:
                self._item_pomo_accion.title = "⏸ Pausar Pomodoro"

        self._item_pomo_sesiones.title = f"Sesiones hoy: {sesiones}"

    def _actualizar_seccion_tareas(self):
        """Reconstruye los items de tareas para el Space actual."""
        with self._lock:
            space_id = self._current_space
        sid = str(space_id)
        nombre = self.nombre_space(space_id)

        # Actualizar header
        self._item_header_tareas.title = f"── Tareas ({nombre}) ──"

        # Eliminar items viejos del menú
        for tid, item in list(self._items_tareas.items()):
            try:
                del self.menu[item.title]
            except Exception:
                pass
        self._items_tareas.clear()

        with self._task_lock:
            tareas = list(self._tareas.get(sid, []))

        # Ordenar: pendientes primero, completadas al final
        pendientes = [t for t in tareas if not t.get("done")]
        completadas = [t for t in tareas if t.get("done")]
        tareas_ord = pendientes + completadas

        for tarea in tareas_ord:
            tid = tarea["id"]
            texto = tarea["text"]
            hecha = tarea["done"]
            check = "☑" if hecha else "☐"
            label = f"{check} {texto}"

            item = rumps.MenuItem(
                label, callback=lambda sender, t=tarea: self._toggle_tarea(t)
            )
            self._items_tareas[tid] = item

            # Insertar antes del botón "+ Agregar tarea..."
            try:
                self.menu.insert_before("+ Agregar tarea...", item)
            except Exception:
                self.menu.add(item)

    # ─────────────────────────────────────────
    # Tick principal (cada 1 segundo)
    # ─────────────────────────────────────────

    def _tick(self, sender):
        """Actualiza todo el estado de la app cada segundo."""
        # Detectar cambio de día
        hoy = datetime.date.today()
        with self._lock:
            cambio_dia = hoy != self._today

        if cambio_dia:
            self._resetear_dia(hoy)

        # Fallback por polling: verificar si el Space cambió
        # (por si NSWorkspace no disparó la notificación)
        space_actual = obtener_space_actual()
        with self._lock:
            space_conocido = self._current_space
        if space_actual != -1 and space_actual != space_conocido:
            self.manejar_cambio_space()

        # Tick del Pomodoro
        self._tick_pomo()

        # Actualizar UI
        self._actualizar_titulo_barra()
        self._actualizar_seccion_espacios()
        self._actualizar_seccion_pomo()

    def _resetear_dia(self, nuevo_dia):
        """Guarda el día anterior y resetea para el nuevo día."""
        # Acumular sesión activa antes de guardar
        with self._lock:
            if not self._is_paused and not self._is_idle_paused:
                elapsed = time.time() - self._session_start
                sid = str(self._current_space)
                self._space_seconds[sid] = self._space_seconds.get(sid, 0.0) + elapsed
                self._session_start = time.time()
            self._today = nuevo_dia

        self._guardar_historial()

        with self._lock:
            self._space_seconds = {}
            self._session_start = time.time()

        with self._pomo_lock:
            self._pomo_sesiones = 0
            self._pomo_sesiones_fecha = str(nuevo_dia)

        self._limpiar_tareas_antiguas()
        self._actualizar_seccion_tareas()

    # ─────────────────────────────────────────
    # Pausa global
    # ─────────────────────────────────────────

    def _toggle_pausa(self, sender):
        """Alterna la pausa global manual."""
        with self._lock:
            if not self._is_paused:
                # Acumular sesión antes de pausar
                elapsed = time.time() - self._session_start
                sid = str(self._current_space)
                self._space_seconds[sid] = self._space_seconds.get(sid, 0.0) + elapsed
                self._is_paused = True
                # Recordar si el pomo estaba activo
                with self._pomo_lock:
                    self._pomo_estaba_activo = (
                        self._pomo_activo and not self._pomo_pausado
                    )
                    if self._pomo_activo and not self._pomo_pausado:
                        self._pomo_pausado = True
                self._item_pausa.title = "▶ Reanudar timer"
            else:
                self._is_paused = False
                self._session_start = time.time()
                # Restaurar pomo si estaba activo
                with self._pomo_lock:
                    if self._pomo_estaba_activo and self._pomo_activo:
                        self._pomo_pausado = False
                        self._pomo_ultimo_tick = time.time()
                self._item_pausa.title = "⏸ Pausar timer"

    # ─────────────────────────────────────────
    # Idle watcher (thread daemon)
    # ─────────────────────────────────────────

    def _iniciar_idle_watcher(self):
        def _watcher():
            while True:
                time.sleep(INTERVALO_IDLE)
                try:
                    idle = get_idle_seconds()
                    with self._lock:
                        pausado_manual = self._is_paused
                        idle_pausado = self._is_idle_paused

                    if not pausado_manual:
                        if not idle_pausado and idle >= UMBRAL_INACTIVIDAD:
                            # Activar pausa automática y descontar el tiempo idle
                            with self._lock:
                                elapsed = time.time() - self._session_start
                                sid = str(self._current_space)
                                # Solo acumular lo real (sin los segundos idle)
                                tiempo_real = max(0, elapsed - UMBRAL_INACTIVIDAD)
                                self._space_seconds[sid] = (
                                    self._space_seconds.get(sid, 0.0) + tiempo_real
                                )
                                self._is_idle_paused = True
                                self._idle_paused_at = time.time()
                        elif idle_pausado and idle < REANUDAR_INACTIVIDAD:
                            # Reanudar
                            with self._lock:
                                self._is_idle_paused = False
                                self._session_start = time.time()
                except Exception as e:
                    print(f"[desktimer] Error en idle watcher: {e}")

        t = threading.Thread(target=_watcher, daemon=True, name="idle-watcher")
        t.start()

    # ─────────────────────────────────────────
    # Autosave (thread daemon)
    # ─────────────────────────────────────────

    def _iniciar_autosave(self):
        def _autosave():
            while True:
                time.sleep(INTERVALO_AUTOSAVE)
                try:
                    self._guardar_todo()
                except Exception as e:
                    print(f"[desktimer] Error en autosave: {e}")

        t = threading.Thread(target=_autosave, daemon=True, name="autosave")
        t.start()

    # ─────────────────────────────────────────
    # Pomodoro
    # ─────────────────────────────────────────

    def _tick_pomo(self):
        """Avanza el Pomodoro un segundo."""
        with self._pomo_lock:
            if not self._pomo_activo or self._pomo_pausado:
                return
            with self._lock:
                timer_pausado = self._is_paused or self._is_idle_paused
            if timer_pausado:
                return

            ahora = time.time()
            if self._pomo_ultimo_tick:
                elapsed = ahora - self._pomo_ultimo_tick
                self._pomo_restante -= elapsed
            self._pomo_ultimo_tick = ahora

            if self._pomo_restante <= 0:
                self._completar_fase_pomo()

    def _completar_fase_pomo(self):
        """Transiciona a la siguiente fase del Pomodoro. Se llama con _pomo_lock."""
        if self._pomo_fase == "focus":
            self._pomo_sesiones += 1
            if self._pomo_sesiones % self._settings["pomo_sessions_for_long"] == 0:
                self._pomo_fase = "long_break"
                self._pomo_restante = self._settings["pomo_long_break"]
                sonido = "Hero"
                msg = f"¡Descanso largo de {self._settings['pomo_long_break'] // 60} minutos!"
                subtitulo = "Descanso largo"
            else:
                self._pomo_fase = "break"
                self._pomo_restante = self._settings["pomo_break"]
                sonido = "Purr"
                msg = (
                    f"¡Tiempo de descanso! {self._settings['pomo_break'] // 60} minutos"
                )
                subtitulo = "Descanso"
        else:
            self._pomo_fase = "focus"
            self._pomo_restante = self._settings["pomo_focus"]
            sonido = "Glass"
            msg = f"¡A enfocarse! {self._settings['pomo_focus'] // 60} minutos"
            subtitulo = "Enfoque"

        self._pomo_ultimo_tick = time.time()

        # Sonido y notificación (fuera del lock para evitar deadlock)
        threading.Thread(
            target=lambda: (
                reproducir_sonido(sonido),
                enviar_notificacion(subtitulo, msg),
            ),
            daemon=True,
        ).start()

    def _toggle_pomo(self, sender):
        """Inicia o pausa el Pomodoro."""
        with self._pomo_lock:
            if not self._pomo_activo:
                # Iniciar
                self._pomo_activo = True
                self._pomo_pausado = False
                self._pomo_fase = "focus"
                self._pomo_restante = self._settings["pomo_focus"]
                self._pomo_ultimo_tick = time.time()
                reproducir_sonido("Glass")
            else:
                # Pausar / reanudar
                if self._pomo_pausado:
                    self._pomo_pausado = False
                    self._pomo_ultimo_tick = time.time()
                else:
                    self._pomo_pausado = True

    def _saltar_fase_pomo(self, sender):
        """Salta a la siguiente fase del Pomodoro."""
        with self._pomo_lock:
            if self._pomo_activo:
                self._pomo_restante = 0
                self._completar_fase_pomo()

    # ─────────────────────────────────────────
    # To-Do list
    # ─────────────────────────────────────────

    def _agregar_tarea(self, sender):
        """Abre un diálogo para agregar una tarea al Space actual."""
        with self._lock:
            space_id = self._current_space
        nombre = self.nombre_space(space_id)

        respuesta = rumps.Window(
            message=f"Agregar tarea en '{nombre}':",
            title="Nueva tarea",
            default_text="",
            ok="Agregar",
            cancel="Cancelar",
            dimensions=(280, 24),
        ).run()

        if respuesta.clicked and respuesta.text.strip():
            nueva = {
                "id": uuid.uuid4().hex[:6],
                "text": respuesta.text.strip(),
                "done": False,
                "pinned": False,
                "date_done": None,
            }
            sid = str(space_id)
            with self._task_lock:
                if sid not in self._tareas:
                    self._tareas[sid] = []
                self._tareas[sid].insert(0, nueva)
            self._actualizar_seccion_tareas()

    def _toggle_tarea(self, tarea):
        """Alterna el estado done/undone de una tarea."""
        with self._lock:
            space_id = self._current_space
        sid = str(space_id)
        hoy_str = str(datetime.date.today())

        with self._task_lock:
            for t in self._tareas.get(sid, []):
                if t["id"] == tarea["id"]:
                    t["done"] = not t["done"]
                    t["date_done"] = hoy_str if t["done"] else None
                    break

        self._actualizar_seccion_tareas()

    # ─────────────────────────────────────────
    # Preferencias y configuración
    # ─────────────────────────────────────────

    def _renombrar_spaces(self, sender):
        """Abre un diálogo por cada Space para renombrarlo."""
        spaces = self._spaces_conocidos()
        for sid in spaces:
            nombre_actual = self._nombres.get(sid, f"Espacio {sid}")
            respuesta = rumps.Window(
                message=f"Nombre para el Space ID {sid}:",
                title="Renombrar Space",
                default_text=nombre_actual,
                ok="Guardar",
                cancel="Cancelar",
                dimensions=(280, 24),
            ).run()

            if respuesta.clicked and respuesta.text.strip():
                self._nombres[sid] = respuesta.text.strip()

        _guardar_json(NOMBRES_PATH, self._nombres)
        self._actualizar_seccion_espacios()
        self._actualizar_seccion_tareas()

    def _configurar_metas(self, sender):
        """Abre un diálogo para configurar metas diarias por Space."""
        spaces = self._spaces_conocidos()
        for sid in spaces:
            nombre = self._nombres.get(sid, f"Espacio {sid}")
            meta_actual = self._metas.get(sid, 0)
            horas_actual = f"{meta_actual // 3600}" if meta_actual else ""

            respuesta = rumps.Window(
                message=f"Meta diaria para '{nombre}' (en horas, 0 = sin meta):",
                title="Configurar meta",
                default_text=horas_actual,
                ok="Guardar",
                cancel="Cancelar",
                dimensions=(280, 24),
            ).run()

            if respuesta.clicked:
                try:
                    horas = float(respuesta.text.strip())
                    if horas > 0:
                        self._metas[sid] = int(horas * 3600)
                    else:
                        self._metas.pop(sid, None)
                except ValueError:
                    pass

        _guardar_json(METAS_PATH, self._metas)
        self._actualizar_seccion_espacios()

    def _abrir_configuracion(self, sender):
        """Abre ventanas para configurar Pomodoro e idle."""
        campos = [
            (
                "pomo_focus",
                "Duración de enfoque (minutos)",
                self._settings["pomo_focus"] // 60,
            ),
            (
                "pomo_break",
                "Duración de descanso (minutos)",
                self._settings["pomo_break"] // 60,
            ),
            (
                "pomo_long_break",
                "Duración de descanso largo (minutos)",
                self._settings["pomo_long_break"] // 60,
            ),
            (
                "pomo_sessions_for_long",
                "Sesiones antes de descanso largo",
                self._settings["pomo_sessions_for_long"],
            ),
            (
                "idle_threshold",
                "Segundos de inactividad para pausar",
                self._settings["idle_threshold"],
            ),
            (
                "idle_resume",
                "Segundos de actividad para reanudar",
                self._settings["idle_resume"],
            ),
        ]

        nuevos = {}
        for clave, label, valor_actual in campos:
            respuesta = rumps.Window(
                message=f"{label}:",
                title="Configuración",
                default_text=str(valor_actual),
                ok="Guardar",
                cancel="Cancelar",
                dimensions=(280, 24),
            ).run()

            if respuesta.clicked:
                try:
                    nuevos[clave] = int(respuesta.text.strip())
                except ValueError:
                    pass

        for clave, val in nuevos.items():
            if clave in ("pomo_focus", "pomo_break", "pomo_long_break"):
                self._settings[clave] = val * 60
            else:
                self._settings[clave] = val

        _guardar_json(SETTINGS_PATH, self._settings)

        # Actualizar umbrales de idle
        global UMBRAL_INACTIVIDAD, REANUDAR_INACTIVIDAD
        UMBRAL_INACTIVIDAD = self._settings.get("idle_threshold", 200)
        REANUDAR_INACTIVIDAD = self._settings.get("idle_resume", 30)

    def _ver_historial(self, sender):
        """Muestra el historial de los últimos 7 días en una ventana."""
        historial = _cargar_json(HISTORIAL_PATH, {})
        hoy = datetime.date.today()
        lineas = []

        for i in range(7):
            dia = hoy - datetime.timedelta(days=i)
            dia_str = str(dia)
            label = (
                "Hoy" if i == 0 else ("Ayer" if i == 1 else dia.strftime("%a %d/%m"))
            )
            datos_dia = historial.get(dia_str, {})

            if not datos_dia:
                lineas.append(f"{label}: sin datos")
                continue

            lineas.append(f"── {label} ({dia_str}) ──")
            total = 0
            for sid, secs in datos_dia.items():
                nombre = self._nombres.get(sid, f"Space {sid}")
                lineas.append(f"  {nombre}: {fmt_tiempo(secs)}")
                total += secs
            lineas.append(f"  TOTAL: {fmt_tiempo(total)}")
            lineas.append("")

        rumps.alert(
            title="Historial — Últimos 7 días",
            message="\n".join(lineas) if lineas else "Sin historial.",
            ok="Cerrar",
        )

    # ─────────────────────────────────────────
    # Salir
    # ─────────────────────────────────────────

    def _salir(self, sender):
        """Guarda todo y cierra la app."""
        # Acumular sesión activa antes de guardar
        with self._lock:
            if not self._is_paused and not self._is_idle_paused:
                elapsed = time.time() - self._session_start
                sid = str(self._current_space)
                self._space_seconds[sid] = self._space_seconds.get(sid, 0.0) + elapsed
        self._guardar_todo()
        rumps.quit_application()

    def _guardar_y_salir(self, signum, frame):
        """Handler de señal SIGTERM/SIGINT para guardar antes de cerrar."""
        with self._lock:
            if not self._is_paused and not self._is_idle_paused:
                elapsed = time.time() - self._session_start
                sid = str(self._current_space)
                self._space_seconds[sid] = self._space_seconds.get(sid, 0.0) + elapsed
        self._guardar_todo()
        raise SystemExit(0)


# ─────────────────────────────────────────────
# Punto de entrada
# ─────────────────────────────────────────────

if __name__ == "__main__":
    app = DeskTimerApp()
    app.run()
