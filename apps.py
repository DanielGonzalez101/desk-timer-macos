# -*- coding: utf-8 -*-
"""
apps.py — Deteccion de la aplicacion activa en macOS.

Usa NSWorkspace (API publica y estable) para obtener la app
en primer plano y recibir notificaciones de cambio.
"""

from typing import Callable, Optional

from AppKit import NSObject, NSWorkspace  # type: ignore

# ─────────────────────────────────────────────
# Obtener app activa
# ─────────────────────────────────────────────


def obtener_app_activa() -> Optional[str]:
    """
    Retorna el nombre de la aplicacion actualmente en primer plano.
    Retorna None si no se puede determinar.

    Ejemplo de retorno: "Figma", "Terminal", "Google Chrome"
    """
    try:
        workspace = NSWorkspace.sharedWorkspace()
        app = workspace.frontmostApplication()
        if app:
            return app.localizedName()
        return None
    except Exception:
        return None


def obtener_bundle_id_activo() -> Optional[str]:
    """
    Retorna el bundle identifier de la app activa.
    Ejemplo: "com.figma.Desktop", "com.apple.Terminal"
    Util para identificar apps de forma unica independientemente del nombre.
    """
    try:
        workspace = NSWorkspace.sharedWorkspace()
        app = workspace.frontmostApplication()
        if app:
            return app.bundleIdentifier()
        return None
    except Exception:
        return None


# ─────────────────────────────────────────────
# Observer de NSWorkspace para cambios de app
# ─────────────────────────────────────────────


class _ObservadorApps(NSObject):
    """
    NSObject que recibe NSWorkspaceDidActivateApplicationNotification.
    Se dispara cada vez que el usuario cambia de aplicacion activa.
    IMPORTANTE: nombres de selectores solo ASCII.
    """

    # Callback: fn(nombre_app: str)
    callback = None  # type: Optional[Callable[[str], None]]

    def appDidActivate_(self, notification):
        if self.callback:
            nombre = obtener_app_activa()
            if nombre:
                self.callback(nombre)


_observer_instance: Optional[_ObservadorApps] = None


def registrar_observer_apps(callback: Callable[[str], None]):
    """
    Registra un callback que se llama cada vez que cambia la app activa.
    El callback recibe el nombre de la nueva app (str).
    Solo se puede registrar un observer a la vez.
    """
    global _observer_instance

    _ObservadorApps.callback = callback
    _observer_instance = _ObservadorApps.alloc().init()

    nc = NSWorkspace.sharedWorkspace().notificationCenter()
    nc.addObserver_selector_name_object_(
        _observer_instance,
        "appDidActivate:",
        "NSWorkspaceDidActivateApplicationNotification",
        None,
    )


def desregistrar_observer_apps():
    """Elimina el observer de Apps."""
    global _observer_instance
    if _observer_instance:
        nc = NSWorkspace.sharedWorkspace().notificationCenter()
        nc.removeObserver_(_observer_instance)
        _observer_instance = None
