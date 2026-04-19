# -*- coding: utf-8 -*-
"""
pomodoro.py — Estado y logica del Pomodoro.

Fases: focus -> break -> focus -> break -> ... -> long_break (cada N sesiones)
"""

import datetime
import threading
import time
from typing import Callable, Optional

from utils import enviar_notificacion, reproducir_sonido

# Emojis e iconos por fase
FASES = {
    "focus": {"icono": "🍅", "label": "Enfoque", "sonido": "Glass"},
    "break": {"icono": "☕", "label": "Descanso", "sonido": "Purr"},
    "long_break": {"icono": "🌴", "label": "Descanso largo", "sonido": "Hero"},
}


class Pomodoro:
    """
    Gestiona el estado del Pomodoro.
    Hilo-seguro via self._lock.
    """

    def __init__(self, settings: dict, on_cambio: Optional[Callable] = None):
        """
        Args:
            settings: dict de configuracion con pomo_focus, pomo_break, etc.
            on_cambio: callback que se llama cuando cambia el estado
                       (para que el menu se actualice)
        """
        self._lock = threading.Lock()
        self._settings = settings
        self._on_cambio = on_cambio

        # Estado
        self._activo: bool = False
        self._pausado: bool = False
        self._fase: str = "focus"
        self._restante: float = float(settings.get("pomo_focus", 1500))
        self._ultimo_tick: Optional[float] = None

        # Sesiones completadas hoy
        self._sesiones: int = 0
        self._sesiones_fecha: str = str(datetime.date.today())

        # Para restaurar el estado tras pausa global del timer
        self._estaba_activo: bool = False

    # ─────────────────────────────────────────
    # Propiedades de solo lectura
    # ─────────────────────────────────────────

    @property
    def activo(self) -> bool:
        with self._lock:
            return self._activo

    @property
    def pausado(self) -> bool:
        with self._lock:
            return self._pausado

    @property
    def fase(self) -> str:
        with self._lock:
            return self._fase

    @property
    def restante(self) -> float:
        with self._lock:
            return self._restante

    @property
    def sesiones(self) -> int:
        with self._lock:
            return self._sesiones

    def info_fase(self) -> dict:
        """Retorna icono, label y segundos restantes de la fase actual."""
        with self._lock:
            return {
                **FASES[self._fase],
                "restante": self._restante,
                "activo": self._activo,
                "pausado": self._pausado,
                "sesiones": self._sesiones,
            }

    # ─────────────────────────────────────────
    # Tick (llamado cada segundo desde menu.py)
    # ─────────────────────────────────────────

    def tick(self, timer_pausado: bool = False):
        """
        Avanza el Pomodoro un segundo.
        timer_pausado: True si el timer global esta pausado (manual o idle).
        """
        with self._lock:
            if not self._activo or self._pausado or timer_pausado:
                self._ultimo_tick = None
                return

            ahora = time.time()
            if self._ultimo_tick is not None:
                self._restante -= ahora - self._ultimo_tick
            self._ultimo_tick = ahora

            if self._restante <= 0:
                self._completar_fase()

    def _completar_fase(self):
        """Transiciona a la siguiente fase. Llamar con self._lock tomado."""
        if self._fase == "focus":
            self._sesiones += 1
            n = self._settings.get("pomo_sessions_for_long", 4)
            if self._sesiones % n == 0:
                self._fase = "long_break"
                self._restante = float(self._settings.get("pomo_long_break", 900))
            else:
                self._fase = "break"
                self._restante = float(self._settings.get("pomo_break", 300))
        else:
            self._fase = "focus"
            self._restante = float(self._settings.get("pomo_focus", 1500))

        self._ultimo_tick = time.time()
        info = FASES[self._fase]

        # Sonido y notificacion en thread separado para no bloquear
        threading.Thread(
            target=lambda: (
                reproducir_sonido(info["sonido"]),
                enviar_notificacion(info["label"], self._mensaje_fase()),
            ),
            daemon=True,
        ).start()

        if self._on_cambio:
            self._on_cambio()

    def _mensaje_fase(self) -> str:
        minutos = int(self._restante) // 60
        if self._fase == "focus":
            return f"A enfocarse — {minutos} minutos"
        elif self._fase == "break":
            return f"Descansa {minutos} minutos"
        else:
            return f"Descanso largo — {minutos} minutos"

    # ─────────────────────────────────────────
    # Controles
    # ─────────────────────────────────────────

    def toggle(self):
        """Inicia, pausa o reanuda el Pomodoro."""
        with self._lock:
            if not self._activo:
                # Iniciar desde cero
                self._activo = True
                self._pausado = False
                self._fase = "focus"
                self._restante = float(self._settings.get("pomo_focus", 1500))
                self._ultimo_tick = time.time()
                threading.Thread(
                    target=lambda: reproducir_sonido("Glass"),
                    daemon=True,
                ).start()
            else:
                self._pausado = not self._pausado
                if not self._pausado:
                    self._ultimo_tick = time.time()

        if self._on_cambio:
            self._on_cambio()

    def saltar_fase(self):
        """Salta a la siguiente fase inmediatamente."""
        with self._lock:
            if self._activo:
                self._restante = 0
                self._completar_fase()

    def detener(self):
        """Detiene y resetea el Pomodoro."""
        with self._lock:
            self._activo = False
            self._pausado = False
            self._fase = "focus"
            self._restante = float(self._settings.get("pomo_focus", 1500))
            self._ultimo_tick = None

        if self._on_cambio:
            self._on_cambio()

    # ─────────────────────────────────────────
    # Integracion con pausa global del timer
    # ─────────────────────────────────────────

    def on_pausa_global(self, pausando: bool):
        """
        Llamado por timer.py cuando el usuario pausa/reanuda el timer global.
        pausando=True -> se esta pausando; False -> se esta reanudando.
        """
        with self._lock:
            if pausando:
                self._estaba_activo = self._activo and not self._pausado
                if self._activo and not self._pausado:
                    self._pausado = True
            else:
                if self._estaba_activo and self._activo:
                    self._pausado = False
                    self._ultimo_tick = time.time()

    # ─────────────────────────────────────────
    # Reset de dia
    # ─────────────────────────────────────────

    def reset_dia(self):
        """Resetea las sesiones al cambiar de dia."""
        with self._lock:
            self._sesiones = 0
            self._sesiones_fecha = str(datetime.date.today())

    # ─────────────────────────────────────────
    # Persistencia (sesiones del dia)
    # ─────────────────────────────────────────

    def cargar_sesiones(self, datos: dict):
        """Carga sesiones guardadas. datos = {"fecha": "YYYY-MM-DD", "count": N}"""
        hoy = str(datetime.date.today())
        if datos.get("fecha") == hoy:
            with self._lock:
                self._sesiones = datos.get("count", 0)

    def datos_sesiones(self) -> dict:
        """Retorna datos de sesiones para guardar en settings."""
        with self._lock:
            return {
                "fecha": str(datetime.date.today()),
                "count": self._sesiones,
            }
