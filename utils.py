# -*- coding: utf-8 -*-
"""
utils.py — Funciones utilitarias de DeskTimer.
Formato de tiempo, sonido y notificaciones.
"""

import subprocess
import threading

# ─────────────────────────────────────────────
# Formato de tiempo
# ─────────────────────────────────────────────


def fmt_tiempo(segundos: float) -> str:
    """Formatea segundos como H:MM:SS."""
    s = max(0, int(segundos))
    h = s // 3600
    m = (s % 3600) // 60
    seg = s % 60
    return f"{h}:{m:02d}:{seg:02d}"


def fmt_pomo(segundos: float) -> str:
    """Formatea segundos del Pomodoro como MM:SS."""
    s = max(0, int(segundos))
    m = s // 60
    seg = s % 60
    return f"{m:02d}:{seg:02d}"


# ─────────────────────────────────────────────
# Sonido
# ─────────────────────────────────────────────


def reproducir_sonido(nombre: str = "Glass"):
    """
    Reproduce un sonido del sistema de macOS.
    Usa NSSound con fallback a afplay.
    Se ejecuta en un thread para no bloquear el hilo principal.
    """

    def _reproducir():
        try:
            from AppKit import NSSound  # type: ignore

            sonido = NSSound.soundNamed_(nombre)
            if sonido:
                sonido.play()
                return
        except Exception:
            pass

        # Fallback: afplay
        try:
            subprocess.Popen(
                ["afplay", f"/System/Library/Sounds/{nombre}.aiff"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception:
            pass

    threading.Thread(target=_reproducir, daemon=True).start()


# ─────────────────────────────────────────────
# Notificaciones
# ─────────────────────────────────────────────


def enviar_notificacion(subtitulo: str, mensaje: str):
    """Envia una notificacion nativa de macOS via rumps."""
    try:
        import rumps  # type: ignore

        rumps.notification(
            title="DeskTimer",
            subtitle=subtitulo,
            message=mensaje,
            sound=False,
        )
    except Exception:
        pass
