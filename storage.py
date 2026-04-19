# -*- coding: utf-8 -*-
"""
storage.py — Lectura y escritura de archivos JSON para DeskTimer.
Todas las operaciones de disco pasan por aqui.
"""

import datetime
import json
import os

from config import (
    APPS_IGNORADAS_PATH,
    DIRECTORIO_DATOS,
    HISTORIAL_APPS_PATH,
    HISTORIAL_SPACES_PATH,
    NOMBRES_SPACES_PATH,
    SETTINGS_DEFAULT,
    SETTINGS_PATH,
    TAREAS_PATH,
)


def inicializar():
    """Crea el directorio de datos si no existe."""
    os.makedirs(DIRECTORIO_DATOS, exist_ok=True)


# ─────────────────────────────────────────────
# Primitivas JSON
# ─────────────────────────────────────────────


def cargar_json(path: str, default):
    """Carga un archivo JSON. Si no existe o esta corrupto, retorna default."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def guardar_json(path: str, datos) -> bool:
    """
    Guarda datos en un archivo JSON de forma atomica (via archivo temporal).
    Retorna True si tuvo exito, False si fallo.
    """
    try:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(datos, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
        return True
    except Exception as e:
        print(f"[storage] Error al guardar {path}: {e}")
        return False


# ─────────────────────────────────────────────
# Settings
# ─────────────────────────────────────────────


def cargar_settings() -> dict:
    """Carga settings fusionados con los defaults."""
    guardados = cargar_json(SETTINGS_PATH, {})
    return {**SETTINGS_DEFAULT, **guardados}


def guardar_settings(settings: dict) -> bool:
    return guardar_json(SETTINGS_PATH, settings)


# ─────────────────────────────────────────────
# Historial de Spaces
# ─────────────────────────────────────────────


def cargar_historial_spaces() -> dict:
    """
    Retorna el historial completo de Spaces.
    Estructura: { "YYYY-MM-DD": { "space_id": segundos, ... }, ... }
    """
    return cargar_json(HISTORIAL_SPACES_PATH, {})


def guardar_historial_spaces(historial: dict) -> bool:
    return guardar_json(HISTORIAL_SPACES_PATH, historial)


def cargar_segundos_spaces_hoy() -> dict:
    """Retorna { str(space_id): float } para el dia de hoy."""
    historial = cargar_historial_spaces()
    hoy = str(datetime.date.today())
    return {k: float(v) for k, v in historial.get(hoy, {}).items()}


def guardar_segundos_spaces_hoy(segundos: dict) -> bool:
    """Guarda los segundos del dia de hoy en el historial de Spaces."""
    historial = cargar_historial_spaces()
    hoy = str(datetime.date.today())
    historial[hoy] = segundos
    return guardar_historial_spaces(historial)


# ─────────────────────────────────────────────
# Historial de Apps
# ─────────────────────────────────────────────


def cargar_historial_apps() -> dict:
    """
    Retorna el historial completo de Apps.
    Estructura: { "YYYY-MM-DD": { "nombre_app": segundos, ... }, ... }
    """
    return cargar_json(HISTORIAL_APPS_PATH, {})


def guardar_historial_apps(historial: dict) -> bool:
    return guardar_json(HISTORIAL_APPS_PATH, historial)


def cargar_segundos_apps_hoy() -> dict:
    """Retorna { nombre_app: float } para el dia de hoy."""
    historial = cargar_historial_apps()
    hoy = str(datetime.date.today())
    return {k: float(v) for k, v in historial.get(hoy, {}).items()}


def guardar_segundos_apps_hoy(segundos: dict) -> bool:
    """Guarda los segundos del dia de hoy en el historial de Apps."""
    historial = cargar_historial_apps()
    hoy = str(datetime.date.today())
    historial[hoy] = segundos
    return guardar_historial_apps(historial)


# ─────────────────────────────────────────────
# Nombres de Spaces
# ─────────────────────────────────────────────


def cargar_nombres_spaces() -> dict:
    """Retorna { str(space_id): nombre }."""
    return cargar_json(NOMBRES_SPACES_PATH, {})


def guardar_nombres_spaces(nombres: dict) -> bool:
    return guardar_json(NOMBRES_SPACES_PATH, nombres)


# ─────────────────────────────────────────────
# Apps ignoradas
# ─────────────────────────────────────────────


def cargar_apps_ignoradas() -> list:
    """Retorna lista de nombres de apps a ignorar."""
    return cargar_json(APPS_IGNORADAS_PATH, [])


def guardar_apps_ignoradas(apps: list) -> bool:
    return guardar_json(APPS_IGNORADAS_PATH, apps)


# ─────────────────────────────────────────────
# Tareas
# ─────────────────────────────────────────────


def cargar_tareas() -> list:
    """
    Retorna la lista global de tareas.
    Estructura: [{ id, text, done, date_done }, ...]
    """
    return cargar_json(TAREAS_PATH, [])


def guardar_tareas(tareas: list) -> bool:
    return guardar_json(TAREAS_PATH, tareas)
