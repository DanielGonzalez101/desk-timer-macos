# -*- coding: utf-8 -*-
"""
spaces.py — Deteccion de Spaces virtuales de macOS.

Usa CoreGraphics (API privada) para obtener el Space actual,
y NSWorkspace para recibir notificaciones de cambio.
"""

import ctypes
import ctypes.util
from typing import Callable, Optional

from AppKit import NSObject, NSWorkspace  # type: ignore

# ─────────────────────────────────────────────
# CoreGraphics — API privada para Space ID
# ─────────────────────────────────────────────


def _cargar_coregraphics():
    """
    Carga las funciones privadas de CoreGraphics para leer el Space actual.
    Retorna (fn_conexion, fn_get_space) o (None, None) si falla.
    """
    try:
        lib = ctypes.util.find_library("CoreGraphics")
        cg = ctypes.cdll.LoadLibrary(lib)

        # El nombre de la funcion varia segun la version de macOS
        fn_conexion = None
        for nombre in ("CGSGetDefaultConnectionForProcess", "_CGSDefaultConnection"):
            try:
                fn = getattr(cg, nombre)
                fn.restype = ctypes.c_int
                _ = fn()  # verificar que funciona
                fn_conexion = fn
                break
            except Exception:
                continue

        if fn_conexion is None:
            print(
                "[spaces] Advertencia: CGSGetDefaultConnectionForProcess no disponible. "
                "Usando modo polling."
            )
            return None, None

        fn_get_space = cg.CGSGetActiveSpace
        fn_get_space.argtypes = [ctypes.c_int]
        fn_get_space.restype = ctypes.c_uint64

        return fn_conexion, fn_get_space

    except Exception as e:
        print(f"[spaces] No se pudo cargar CoreGraphics: {e}")
        return None, None


_fn_conexion, _fn_get_space = _cargar_coregraphics()


def obtener_space_actual() -> int:
    """
    Retorna el ID del Space activo.
    Retorna -1 si no se puede determinar.
    """
    if _fn_conexion is None or _fn_get_space is None:
        return -1
    try:
        conn = _fn_conexion()
        return int(_fn_get_space(conn))
    except Exception:
        return -1


# ─────────────────────────────────────────────
# Observer de NSWorkspace para cambios de Space
# ─────────────────────────────────────────────


class _ObservadorSpaces(NSObject):
    """
    NSObject que recibe la notificacion NSWorkspaceActiveSpaceDidChangeNotification.
    IMPORTANTE: los selectores de Objective-C solo aceptan ASCII en el nombre.
    """

    # Callback a llamar cuando cambia el Space: fn(nuevo_space_id: int)
    callback = None  # type: Optional[Callable[[int], None]]

    def spaceDidChange_(self, notification):
        if self.callback:
            nuevo_id = obtener_space_actual()
            self.callback(nuevo_id)


_observer_instance: Optional[_ObservadorSpaces] = None


def registrar_observer_spaces(callback: Callable[[int], None]):
    """
    Registra un callback que se llama cada vez que el usuario cambia de Space.
    El callback recibe el nuevo space_id (int).
    Solo se puede registrar un observer a la vez.
    """
    global _observer_instance

    _ObservadorSpaces.callback = callback
    _observer_instance = _ObservadorSpaces.alloc().init()

    nc = NSWorkspace.sharedWorkspace().notificationCenter()
    nc.addObserver_selector_name_object_(
        _observer_instance,
        "spaceDidChange:",
        "NSWorkspaceActiveSpaceDidChangeNotification",
        None,
    )


def desregistrar_observer_spaces():
    """Elimina el observer de Spaces."""
    global _observer_instance
    if _observer_instance:
        nc = NSWorkspace.sharedWorkspace().notificationCenter()
        nc.removeObserver_(_observer_instance)
        _observer_instance = None
