# -*- coding: utf-8 -*-
"""
__main__.py — Punto de entrada de DeskTimer.

Uso:
    python3 __main__.py
"""

import os
import sys

# Asegurar que la carpeta del proyecto este en el path
# para que los imports funcionen al correr directamente
sys.path.insert(0, os.path.dirname(__file__))

from menu import DeskTimerApp

if __name__ == "__main__":
    app = DeskTimerApp()
    app.run()
