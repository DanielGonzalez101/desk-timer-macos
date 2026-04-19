# -*- coding: utf-8 -*-
"""
config.py — Constantes, paths y settings por defecto de DeskTimer.
"""

import os

# ─────────────────────────────────────────────
# Directorio de datos
# ─────────────────────────────────────────────

DIRECTORIO_DATOS = os.path.expanduser("~/.desktimer")

# Paths de archivos JSON
HISTORIAL_SPACES_PATH = os.path.join(DIRECTORIO_DATOS, "historial_spaces.json")
HISTORIAL_APPS_PATH = os.path.join(DIRECTORIO_DATOS, "historial_apps.json")
NOMBRES_SPACES_PATH = os.path.join(DIRECTORIO_DATOS, "nombres_spaces.json")
TAREAS_PATH = os.path.join(DIRECTORIO_DATOS, "tareas.json")
SETTINGS_PATH = os.path.join(DIRECTORIO_DATOS, "settings.json")
APPS_IGNORADAS_PATH = os.path.join(DIRECTORIO_DATOS, "apps_ignoradas.json")

# ─────────────────────────────────────────────
# Modos de tracking
# ─────────────────────────────────────────────

MODO_SPACES = "spaces"
MODO_APPS = "apps"

# ─────────────────────────────────────────────
# Intervalos (segundos)
# ─────────────────────────────────────────────

INTERVALO_TICK = 1  # tick principal del timer
INTERVALO_AUTOSAVE = 30  # guardado automatico
INTERVALO_IDLE = 10  # verificacion de inactividad

# ─────────────────────────────────────────────
# Umbrales de inactividad (segundos)
# ─────────────────────────────────────────────

UMBRAL_INACTIVIDAD = 200  # sin input -> pausa automatica
REANUDAR_INACTIVIDAD = 30  # actividad detectada -> reanudar

# ─────────────────────────────────────────────
# Settings por defecto
# ─────────────────────────────────────────────

SETTINGS_DEFAULT = {
    "modo": MODO_APPS,
    "pomo_focus": 1500,  # 25 minutos
    "pomo_break": 300,  # 5 minutos
    "pomo_long_break": 900,  # 15 minutos
    "pomo_sessions_for_long": 4,
    "idle_threshold": UMBRAL_INACTIVIDAD,
    "idle_resume": REANUDAR_INACTIVIDAD,
}
