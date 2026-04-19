# -*- coding: utf-8 -*-
"""
timer.py — Nucleo del tracker pasivo de tiempo.

Gestiona:
- Acumulacion de segundos por contexto (Space o App)
- Cambio de modo (spaces <-> apps)
- Pausa global manual
- Pausa automatica por inactividad (idle)
- Deteccion de sleep/wake
- Reset automatico al cambiar de dia
- Autosave cada 30 segundos
"""

import datetime
import threading
import time
from typing import Callable, Optional

import Quartz  # type: ignore

import apps as apps_mod
import spaces as spaces_mod
import storage
from config import INTERVALO_AUTOSAVE, INTERVALO_IDLE, MODO_APPS, MODO_SPACES


class Timer:
    """
    Nucleo del tracker de tiempo.
    Hilo-seguro: todas las operaciones de estado usan self._lock.
    """

    def __init__(self, settings: dict, on_contexto_cambiado: Optional[Callable] = None):
        """
        Args:
            settings: dict de configuracion (de storage.cargar_settings())
            on_contexto_cambiado: callback opcional que se llama cuando
                                  cambia el contexto activo (para actualizar el menu)
        """
        self._lock = threading.Lock()
        self._settings = settings
        self._on_contexto_cambiado = on_contexto_cambiado

        # Modo activo
        self._modo: str = settings.get("modo", MODO_APPS)

        # Contexto activo (space_id como int, o nombre de app como str)
        self._contexto_actual: Optional[str] = None

        # Segundos acumulados hoy: { contexto: float }
        self._segundos: dict = {}

        # Timestamp de inicio de la sesion activa
        self._session_start: float = time.time()

        # Flags de pausa
        self._pausado_manual: bool = False
        self._pausado_idle: bool = False

        # Para restaurar el pomodoro tras pausa global
        self._pomo_estaba_activo: bool = False

        # Fecha actual (para detectar cambio de dia)
        self._hoy: datetime.date = datetime.date.today()

        # Callback que el menu puede asignar para pausar/reanudar el pomo
        self.on_pausa_global: Optional[Callable[[bool], None]] = None

        # Cargar historial del dia
        self._cargar_hoy()

        # Registrar observer de sleep/wake
        self._registrar_observer_sistema()

        # Iniciar threads daemon
        self._iniciar_autosave()
        self._iniciar_idle_watcher()

    # ─────────────────────────────────────────
    # Inicializacion
    # ─────────────────────────────────────────

    def _cargar_hoy(self):
        """Carga los segundos acumulados del dia de hoy segun el modo."""
        if self._modo == MODO_SPACES:
            self._segundos = storage.cargar_segundos_spaces_hoy()
        else:
            self._segundos = storage.cargar_segundos_apps_hoy()

    def iniciar_tracking(self):
        """
        Detecta el contexto inicial y registra los observers.
        Llamar despues de que el run loop de Cocoa este activo
        (es decir, despues de que rumps este corriendo).
        """
        if self._modo == MODO_SPACES:
            self._iniciar_modo_spaces()
        else:
            self._iniciar_modo_apps()

    def _iniciar_modo_spaces(self):
        contexto = str(spaces_mod.obtener_space_actual())
        with self._lock:
            self._contexto_actual = contexto
            if contexto not in self._segundos:
                self._segundos[contexto] = 0.0
            self._session_start = time.time()
        spaces_mod.registrar_observer_spaces(self._handle_cambio_space)

    def _iniciar_modo_apps(self):
        nombre = apps_mod.obtener_app_activa() or "Desconocida"
        with self._lock:
            self._contexto_actual = nombre
            if nombre not in self._segundos:
                self._segundos[nombre] = 0.0
            self._session_start = time.time()
        apps_mod.registrar_observer_apps(self._handle_cambio_app)

    # ─────────────────────────────────────────
    # Cambio de modo
    # ─────────────────────────────────────────

    def cambiar_modo(self, nuevo_modo: str):
        """Cambia entre MODO_SPACES y MODO_APPS en caliente."""
        if nuevo_modo == self._modo:
            return

        # Acumular sesion activa antes de cambiar
        self._acumular_sesion_activa()

        # Guardar historial del modo anterior
        self._guardar_hoy()

        # Desregistrar observer anterior
        if self._modo == MODO_SPACES:
            spaces_mod.desregistrar_observer_spaces()
        else:
            apps_mod.desregistrar_observer_apps()

        # Cambiar modo y cargar historial nuevo
        with self._lock:
            self._modo = nuevo_modo
        self._settings["modo"] = nuevo_modo
        storage.guardar_settings(self._settings)

        self._cargar_hoy()

        # Registrar nuevo observer
        if nuevo_modo == MODO_SPACES:
            self._iniciar_modo_spaces()
        else:
            self._iniciar_modo_apps()

        if self._on_contexto_cambiado:
            self._on_contexto_cambiado()

    # ─────────────────────────────────────────
    # Handlers de cambio de contexto
    # ─────────────────────────────────────────

    def _handle_cambio_space(self, nuevo_space_id: int):
        """Llamado por spaces.py cuando el usuario cambia de Space."""
        nuevo = str(nuevo_space_id)
        self._cambiar_contexto(nuevo)

    def _handle_cambio_app(self, nombre_app: str):
        """Llamado por apps.py cuando el usuario cambia de app activa."""
        # Ignorar apps en la lista de ignoradas
        ignoradas = storage.cargar_apps_ignoradas()
        if nombre_app in ignoradas:
            return
        self._cambiar_contexto(nombre_app)

    def _cambiar_contexto(self, nuevo_contexto: str):
        """Acumula el tiempo del contexto anterior y comienza el nuevo."""
        with self._lock:
            if nuevo_contexto == self._contexto_actual:
                return

            # Acumular tiempo del contexto anterior
            if not self._pausado_manual and not self._pausado_idle:
                elapsed = time.time() - self._session_start
                if self._contexto_actual:
                    self._segundos[self._contexto_actual] = (
                        self._segundos.get(self._contexto_actual, 0.0) + elapsed
                    )

            # Iniciar nuevo contexto
            self._contexto_actual = nuevo_contexto
            if nuevo_contexto not in self._segundos:
                self._segundos[nuevo_contexto] = 0.0
            self._session_start = time.time()

        if self._on_contexto_cambiado:
            self._on_contexto_cambiado()

    # ─────────────────────────────────────────
    # Tick principal (llamado cada segundo desde menu.py)
    # ─────────────────────────────────────────

    def tick(self):
        """
        Debe llamarse cada segundo desde el timer de rumps.
        Detecta cambio de dia y hace polling de fallback del contexto.
        """
        # Detectar cambio de dia
        hoy = datetime.date.today()
        with self._lock:
            cambio_dia = hoy != self._hoy

        if cambio_dia:
            self._resetear_dia(hoy)
            return

        # Fallback polling: verificar si el contexto cambio
        # (por si el observer no disparo, e.g. apps en fullscreen)
        if self._modo == MODO_SPACES:
            space_id = spaces_mod.obtener_space_actual()
            if space_id != -1:
                self._handle_cambio_space(space_id)
        else:
            nombre = apps_mod.obtener_app_activa()
            if nombre:
                self._handle_cambio_app(nombre)

    # ─────────────────────────────────────────
    # Pausa global manual
    # ─────────────────────────────────────────

    def toggle_pausa(self):
        """Alterna la pausa global manual."""
        with self._lock:
            if not self._pausado_manual:
                self._acumular_sesion_activa_unsafe()
                self._pausado_manual = True
                pausando = True
            else:
                self._pausado_manual = False
                self._session_start = time.time()
                pausando = False

        # Notificar al pomodoro
        if self.on_pausa_global:
            self.on_pausa_global(pausando)

    @property
    def pausado(self) -> bool:
        with self._lock:
            return self._pausado_manual

    @property
    def idle_pausado(self) -> bool:
        with self._lock:
            return self._pausado_idle

    # ─────────────────────────────────────────
    # Tiempo en vivo
    # ─────────────────────────────────────────

    def segundos_vivos(self, contexto: str) -> float:
        """
        Retorna el tiempo acumulado + sesion activa de un contexto
        sin modificar el estado interno.
        """
        with self._lock:
            base = self._segundos.get(contexto, 0.0)
            es_activo = (
                contexto == self._contexto_actual
                and not self._pausado_manual
                and not self._pausado_idle
            )
            if es_activo:
                base += time.time() - self._session_start
        return base

    def contexto_actual(self) -> Optional[str]:
        with self._lock:
            return self._contexto_actual

    def modo(self) -> str:
        with self._lock:
            return self._modo

    def contextos_conocidos(self) -> list:
        """Retorna lista de contextos ordenados por tiempo acumulado (mayor primero)."""
        with self._lock:
            items = list(self._segundos.items())
        return [k for k, _ in sorted(items, key=lambda x: x[1], reverse=True)]

    # ─────────────────────────────────────────
    # Reset de dia
    # ─────────────────────────────────────────

    def _resetear_dia(self, nuevo_dia: datetime.date):
        """Guarda el dia anterior y resetea para el nuevo dia."""
        self._acumular_sesion_activa()
        self._guardar_hoy()

        with self._lock:
            self._hoy = nuevo_dia
            self._segundos = {}
            self._session_start = time.time()

    # ─────────────────────────────────────────
    # Guardado
    # ─────────────────────────────────────────

    def _acumular_sesion_activa(self):
        with self._lock:
            self._acumular_sesion_activa_unsafe()

    def _acumular_sesion_activa_unsafe(self):
        """Acumula la sesion activa. Llamar con self._lock tomado."""
        if self._pausado_manual or self._pausado_idle:
            return
        if self._contexto_actual is None:
            return
        elapsed = time.time() - self._session_start
        self._segundos[self._contexto_actual] = (
            self._segundos.get(self._contexto_actual, 0.0) + elapsed
        )
        self._session_start = time.time()

    def _guardar_hoy(self):
        with self._lock:
            modo = self._modo
            segundos = dict(self._segundos)

        if modo == MODO_SPACES:
            storage.guardar_segundos_spaces_hoy(segundos)
        else:
            storage.guardar_segundos_apps_hoy(segundos)

    def guardar_todo(self):
        """Guarda el estado actual. Llamar antes de cerrar la app."""
        self._acumular_sesion_activa()
        self._guardar_hoy()

    # ─────────────────────────────────────────
    # Autosave (thread daemon)
    # ─────────────────────────────────────────

    def _iniciar_autosave(self):
        def _loop():
            while True:
                time.sleep(INTERVALO_AUTOSAVE)
                try:
                    self._acumular_sesion_activa()
                    self._guardar_hoy()
                except Exception as e:
                    print(f"[timer] Error en autosave: {e}")

        t = threading.Thread(target=_loop, daemon=True, name="desktimer-autosave")
        t.start()

    # ─────────────────────────────────────────
    # Idle watcher (thread daemon)
    # ─────────────────────────────────────────

    def _iniciar_idle_watcher(self):
        def _loop():
            while True:
                time.sleep(INTERVALO_IDLE)
                try:
                    idle = self._get_idle_seconds()
                    umbral = self._settings.get("idle_threshold", 200)
                    reanudar = self._settings.get("idle_resume", 30)

                    with self._lock:
                        pausado_manual = self._pausado_manual
                        idle_pausado = self._pausado_idle

                    if pausado_manual:
                        continue

                    if not idle_pausado and idle >= umbral:
                        # Activar pausa idle, descontando el tiempo inactivo
                        with self._lock:
                            if not self._pausado_manual and not self._pausado_idle:
                                elapsed = time.time() - self._session_start
                                tiempo_real = max(0.0, elapsed - umbral)
                                if self._contexto_actual:
                                    self._segundos[self._contexto_actual] = (
                                        self._segundos.get(self._contexto_actual, 0.0)
                                        + tiempo_real
                                    )
                                self._pausado_idle = True

                    elif idle_pausado and idle < reanudar:
                        # Reanudar
                        with self._lock:
                            self._pausado_idle = False
                            self._session_start = time.time()

                except Exception as e:
                    print(f"[timer] Error en idle watcher: {e}")

        t = threading.Thread(target=_loop, daemon=True, name="desktimer-idle")
        t.start()

    @staticmethod
    def _get_idle_seconds() -> float:
        try:
            return Quartz.CGEventSourceSecondsSinceLastEventType(
                Quartz.kCGEventSourceStateCombinedSessionState,
                Quartz.kCGAnyInputEventType,
            )
        except Exception:
            return 0.0

    # ─────────────────────────────────────────
    # Observer de sleep/wake del sistema
    # ─────────────────────────────────────────

    def _registrar_observer_sistema(self):
        """Registra notificacion de wake para manejar sleep correctamente."""
        try:
            from AppKit import NSObject, NSWorkspace  # type: ignore

            timer_ref = self

            class _ObservadorSistema(NSObject):
                def sistemaDespierto_(self, notification):
                    # Al despertar, resetear session_start para no contar el sleep
                    hoy = datetime.date.today()
                    with timer_ref._lock:
                        if hoy != timer_ref._hoy:
                            # Cambio de dia durante el sleep
                            timer_ref._hoy = hoy
                            timer_ref._segundos = {}
                        timer_ref._session_start = time.time()
                        timer_ref._pausado_idle = False

            self._obs_sistema = _ObservadorSistema.alloc().init()
            nc = NSWorkspace.sharedWorkspace().notificationCenter()
            nc.addObserver_selector_name_object_(
                self._obs_sistema,
                "sistemaDespierto:",
                "NSWorkspaceDidWakeNotification",
                None,
            )
        except Exception as e:
            print(f"[timer] No se pudo registrar observer de wake: {e}")
