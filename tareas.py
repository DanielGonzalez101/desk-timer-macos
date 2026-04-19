# -*- coding: utf-8 -*-
"""
tareas.py — Checklist global de tareas.

Lista simple de tareas pendientes/completadas, sin contexto.
Las tareas completadas se limpian automaticamente al dia siguiente.
"""

import datetime
import threading
import uuid
from typing import Optional

import storage


class Tareas:
    """
    Gestiona el checklist global de tareas.
    Hilo-seguro via self._lock.
    """

    def __init__(self, on_cambio=None):
        """
        Args:
            on_cambio: callback opcional que se llama al modificar la lista
                       (para que el menu se actualice)
        """
        self._lock = threading.Lock()
        self._on_cambio = on_cambio
        self._tareas: list = []
        self._cargar()

    # ─────────────────────────────────────────
    # Carga y guardado
    # ─────────────────────────────────────────

    def _cargar(self):
        """Carga tareas desde disco y limpia las completadas de dias anteriores."""
        tareas = storage.cargar_tareas()
        hoy = str(datetime.date.today())
        with self._lock:
            self._tareas = [
                t for t in tareas if not t.get("done") or t.get("date_done") == hoy
            ]

    def guardar(self):
        """Guarda el estado actual en disco."""
        with self._lock:
            copia = list(self._tareas)
        storage.guardar_tareas(copia)

    # ─────────────────────────────────────────
    # Consultas
    # ─────────────────────────────────────────

    def lista(self) -> list:
        """
        Retorna copia de la lista ordenada:
        pendientes primero (orden de creacion), completadas al final.
        """
        with self._lock:
            pendientes = [t for t in self._tareas if not t.get("done")]
            completadas = [t for t in self._tareas if t.get("done")]
        return pendientes + completadas

    def total_pendientes(self) -> int:
        with self._lock:
            return sum(1 for t in self._tareas if not t.get("done"))

    # ─────────────────────────────────────────
    # Modificaciones
    # ─────────────────────────────────────────

    def agregar(self, texto: str) -> Optional[dict]:
        """Agrega una tarea nueva. Retorna la tarea creada o None si el texto esta vacio."""
        texto = texto.strip()
        if not texto:
            return None

        nueva = {
            "id": uuid.uuid4().hex[:6],
            "text": texto,
            "done": False,
            "date_done": None,
        }
        with self._lock:
            self._tareas.insert(0, nueva)

        self.guardar()
        if self._on_cambio:
            self._on_cambio()
        return nueva

    def toggle(self, tarea_id: str) -> bool:
        """
        Alterna el estado done/undone de una tarea.
        Retorna True si encontro la tarea, False si no.
        """
        hoy = str(datetime.date.today())
        encontrada = False

        with self._lock:
            for t in self._tareas:
                if t["id"] == tarea_id:
                    t["done"] = not t["done"]
                    t["date_done"] = hoy if t["done"] else None
                    encontrada = True
                    break

        if encontrada:
            self.guardar()
            if self._on_cambio:
                self._on_cambio()

        return encontrada

    def eliminar(self, tarea_id: str) -> bool:
        """
        Elimina una tarea por su ID.
        Retorna True si la elimino, False si no la encontro.
        """
        with self._lock:
            antes = len(self._tareas)
            self._tareas = [t for t in self._tareas if t["id"] != tarea_id]
            eliminada = len(self._tareas) < antes

        if eliminada:
            self.guardar()
            if self._on_cambio:
                self._on_cambio()

        return eliminada

    def editar(self, tarea_id: str, nuevo_texto: str) -> bool:
        """
        Edita el texto de una tarea.
        Retorna True si la encontro, False si no.
        """
        nuevo_texto = nuevo_texto.strip()
        if not nuevo_texto:
            return False

        encontrada = False
        with self._lock:
            for t in self._tareas:
                if t["id"] == tarea_id:
                    t["text"] = nuevo_texto
                    encontrada = True
                    break

        if encontrada:
            self.guardar()
            if self._on_cambio:
                self._on_cambio()

        return encontrada

    def limpiar_completadas(self):
        """Elimina todas las tareas completadas."""
        with self._lock:
            self._tareas = [t for t in self._tareas if not t.get("done")]

        self.guardar()
        if self._on_cambio:
            self._on_cambio()

    def reset_dia(self):
        """Limpia tareas completadas de dias anteriores. Llamar al cambiar de dia."""
        hoy = str(datetime.date.today())
        with self._lock:
            self._tareas = [
                t
                for t in self._tareas
                if not t.get("done") or t.get("date_done") == hoy
            ]
        self.guardar()
