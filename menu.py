# -*- coding: utf-8 -*-
"""
menu.py — App de barra de menu (rumps) y orquestacion de todos los modulos.

DeskTimerApp es la clase principal. Instancia y conecta:
- Timer (tracker pasivo)
- Pomodoro
- Tareas (checklist global)
y construye/actualiza el menu de macOS.
"""

import datetime
import signal

import rumps  # type: ignore

import apps as apps_mod
import spaces as spaces_mod
import storage
from config import MODO_APPS, MODO_SPACES
from pomodoro import Pomodoro
from tareas import Tareas
from timer import Timer
from utils import fmt_pomo, fmt_tiempo


class DeskTimerApp(rumps.App):

    def __init__(self):
        super().__init__("⏱ Cargando...", quit_button=None)

        storage.inicializar()

        # ── Cargar settings ──
        self._settings = storage.cargar_settings()

        # ── Modulos ──
        self._timer = Timer(
            settings=self._settings,
            on_contexto_cambiado=self._on_contexto_cambiado,
        )
        self._pomo = Pomodoro(
            settings=self._settings,
            on_cambio=self._actualizar_seccion_pomo,
        )
        self._tareas = Tareas(
            on_cambio=self._actualizar_seccion_tareas,
        )

        # Conectar pausa global del timer con el pomodoro
        self._timer.on_pausa_global = self._pomo.on_pausa_global

        # ── Construir menu ──
        self._construir_menu()

        # ── Timer principal (1 segundo) ──
        self._tick_timer = rumps.Timer(self._tick, 1)
        self._tick_timer.start()

        # ── Manejo de senales para guardar al cerrar ──
        signal.signal(signal.SIGTERM, self._signal_salir)
        signal.signal(signal.SIGINT, self._signal_salir)

    # ─────────────────────────────────────────
    # Inicio del tracking (post run-loop)
    # ─────────────────────────────────────────

    @rumps.clicked("__init_tracking__")
    def _placeholder(self, _):
        pass

    def application_did_finish_launching(self, notification):
        """Llamado por Cocoa cuando el run loop esta listo."""
        self._timer.iniciar_tracking()
        self._actualizar_todo()

    # ─────────────────────────────────────────
    # Construccion del menu
    # ─────────────────────────────────────────

    def _construir_menu(self):
        """Construye la estructura fija del menu. Solo se llama una vez."""
        self.menu.clear()

        modo = self._settings.get("modo", MODO_APPS)

        # ── Seccion: contextos (Spaces o Apps) ──
        label_seccion = "── Spaces ──" if modo == MODO_SPACES else "── Aplicaciones ──"
        self._item_header_contextos = rumps.MenuItem(label_seccion, callback=None)
        self.menu.add(self._item_header_contextos)

        # Items de contextos (se rellenan dinamicamente)
        self._items_contextos: dict = {}  # contexto -> MenuItem

        self.menu.add(rumps.separator)

        # ── Seccion: Pomodoro ──
        self.menu.add(rumps.MenuItem("── Pomodoro ──", callback=None))
        self._item_pomo_estado = rumps.MenuItem("🍅 Pomodoro inactivo", callback=None)
        self._item_pomo_accion = rumps.MenuItem("▶ Iniciar", callback=self._toggle_pomo)
        self._item_pomo_saltar = rumps.MenuItem(
            "⏭ Saltar fase", callback=self._saltar_pomo
        )
        self._item_pomo_sesiones = rumps.MenuItem("Sesiones hoy: 0", callback=None)
        self.menu.add(self._item_pomo_estado)
        self.menu.add(self._item_pomo_accion)
        self.menu.add(self._item_pomo_saltar)
        self.menu.add(self._item_pomo_sesiones)

        self.menu.add(rumps.separator)

        # ── Seccion: Tareas ──
        self.menu.add(rumps.MenuItem("── Tareas ──", callback=None))
        self._items_tareas: dict = {}  # id_tarea -> MenuItem
        self._item_agregar_tarea = rumps.MenuItem(
            "+ Agregar tarea...", callback=self._agregar_tarea
        )
        self.menu.add(self._item_agregar_tarea)

        self.menu.add(rumps.separator)

        # ── Controles globales ──
        self._item_pausa = rumps.MenuItem("⏸ Pausar timer", callback=self._toggle_pausa)
        self.menu.add(self._item_pausa)

        self.menu.add(rumps.separator)

        # ── Modo ──
        self.menu.add(rumps.MenuItem("── Modo de tracking ──", callback=None))
        self._item_modo_apps = rumps.MenuItem(
            self._label_modo(MODO_APPS),
            callback=lambda _: self._cambiar_modo(MODO_APPS),
        )
        self._item_modo_spaces = rumps.MenuItem(
            self._label_modo(MODO_SPACES),
            callback=lambda _: self._cambiar_modo(MODO_SPACES),
        )
        self.menu.add(self._item_modo_apps)
        self.menu.add(self._item_modo_spaces)

        self.menu.add(rumps.separator)

        # ── Preferencias ──
        self.menu.add(
            rumps.MenuItem(
                "Ignorar aplicaciones...", callback=self._gestionar_apps_ignoradas
            )
        )
        self.menu.add(
            rumps.MenuItem("Renombrar Spaces...", callback=self._renombrar_spaces)
        )
        self.menu.add(
            rumps.MenuItem("Configuracion Pomodoro...", callback=self._config_pomodoro)
        )
        self.menu.add(rumps.MenuItem("Ver historial...", callback=self._ver_historial))

        self.menu.add(rumps.separator)
        self.menu.add(rumps.MenuItem("Salir", callback=self._salir))

        # Render inicial
        self._actualizar_todo()

    def _label_modo(self, modo: str) -> str:
        actual = self._settings.get("modo", MODO_APPS)
        check = "● " if modo == actual else "  "
        nombre = "Apps" if modo == MODO_APPS else "Spaces"
        return f"{check}{nombre}"

    # ─────────────────────────────────────────
    # Tick principal
    # ─────────────────────────────────────────

    def _tick(self, sender):
        """Llamado cada segundo por rumps.Timer."""
        self._timer.tick()
        self._pomo.tick(timer_pausado=self._timer.pausado or self._timer.idle_pausado)
        self._actualizar_titulo()
        self._actualizar_seccion_contextos()
        self._actualizar_seccion_pomo()

    # ─────────────────────────────────────────
    # Actualizacion del titulo de la barra
    # ─────────────────────────────────────────

    def _actualizar_titulo(self):
        if self._timer.pausado:
            self.title = "⏱ ⏸ Pausado"
            return
        if self._timer.idle_pausado:
            self.title = "⏱ 💤 Inactivo"
            return

        contexto = self._timer.contexto_actual()
        if contexto is None:
            self.title = "⏱ ..."
            return

        secs = self._timer.segundos_vivos(contexto)
        nombre = self._nombre_contexto(contexto)
        pomo = "🍅 " if self._pomo.activo and not self._pomo.pausado else ""
        self.title = f"⏱ {pomo}{nombre} {fmt_tiempo(secs)}"

    # ─────────────────────────────────────────
    # Actualizacion de secciones
    # ─────────────────────────────────────────

    def _actualizar_todo(self):
        self._actualizar_titulo()
        self._actualizar_seccion_contextos()
        self._actualizar_seccion_pomo()
        self._actualizar_seccion_tareas()

    def _actualizar_seccion_contextos(self):
        """Actualiza los items de tiempo por contexto."""
        contexto_actual = self._timer.contexto_actual()
        conocidos = self._timer.contextos_conocidos()

        # Crear items nuevos si aparecieron contextos nuevos
        for ctx in conocidos:
            if ctx not in self._items_contextos:
                item = rumps.MenuItem("", callback=None)
                self._items_contextos[ctx] = item
                try:
                    self.menu.insert_after(self._item_header_contextos.title, item)
                except Exception:
                    pass

        # Actualizar titulos
        for ctx, item in self._items_contextos.items():
            secs = self._timer.segundos_vivos(ctx)
            nombre = self._nombre_contexto(ctx)
            marcador = "● " if ctx == contexto_actual else "  "
            item.title = f"{marcador}{nombre:<20} {fmt_tiempo(secs)}"

    def _actualizar_seccion_pomo(self):
        """Actualiza los items del Pomodoro."""
        info = self._pomo.info_fase()

        if not info["activo"]:
            self._item_pomo_estado.title = "🍅 Pomodoro inactivo"
            self._item_pomo_accion.title = "▶ Iniciar"
        else:
            icono = info["icono"]
            label = info["label"]
            self._item_pomo_estado.title = (
                f"{icono} {label}: {fmt_pomo(info['restante'])}"
            )
            self._item_pomo_accion.title = (
                "▶ Reanudar Pomodoro" if info["pausado"] else "⏸ Pausar Pomodoro"
            )

        self._item_pomo_sesiones.title = f"Sesiones hoy: {info['sesiones']}"

    def _actualizar_seccion_tareas(self):
        """Reconstruye los items de tareas."""
        # Eliminar items viejos
        for tid, item in list(self._items_tareas.items()):
            try:
                del self.menu[item.title]
            except Exception:
                pass
        self._items_tareas.clear()

        # Agregar items actuales
        for tarea in self._tareas.lista():
            tid = tarea["id"]
            check = "☑" if tarea["done"] else "☐"
            label = f"{check} {tarea['text']}"

            item = rumps.MenuItem(
                label, callback=lambda _, t=tarea: self._toggle_tarea(t["id"])
            )
            self._items_tareas[tid] = item
            try:
                self.menu.insert_before("+ Agregar tarea...", item)
            except Exception:
                pass

    # ─────────────────────────────────────────
    # Helpers
    # ─────────────────────────────────────────

    def _nombre_contexto(self, contexto: str) -> str:
        """Retorna el nombre legible de un contexto."""
        modo = self._timer.modo()
        if modo == MODO_SPACES:
            nombres = storage.cargar_nombres_spaces()
            return nombres.get(contexto, f"Space {contexto}")
        else:
            return contexto  # En modo apps el nombre es el nombre de la app

    def _on_contexto_cambiado(self):
        """Llamado por timer cuando cambia el contexto activo."""
        self._actualizar_seccion_contextos()
        self._actualizar_titulo()

    # ─────────────────────────────────────────
    # Acciones del menu
    # ─────────────────────────────────────────

    def _toggle_pausa(self, sender):
        self._timer.toggle_pausa()
        self._item_pausa.title = (
            "▶ Reanudar timer" if self._timer.pausado else "⏸ Pausar timer"
        )

    def _toggle_pomo(self, sender):
        self._pomo.toggle()

    def _saltar_pomo(self, sender):
        self._pomo.saltar_fase()

    def _toggle_tarea(self, tarea_id: str):
        self._tareas.toggle(tarea_id)

    def _agregar_tarea(self, sender):
        respuesta = rumps.Window(
            message="Nueva tarea:",
            title="Agregar tarea",
            default_text="",
            ok="Agregar",
            cancel="Cancelar",
            dimensions=(280, 24),
        ).run()

        if respuesta.clicked and respuesta.text.strip():
            self._tareas.agregar(respuesta.text.strip())

    # ─────────────────────────────────────────
    # Cambio de modo
    # ─────────────────────────────────────────

    def _cambiar_modo(self, nuevo_modo: str):
        self._timer.cambiar_modo(nuevo_modo)
        self._settings["modo"] = nuevo_modo

        # Actualizar checkmarks en el menu
        self._item_modo_apps.title = self._label_modo(MODO_APPS)
        self._item_modo_spaces.title = self._label_modo(MODO_SPACES)

        # Actualizar header de la seccion
        label = "── Spaces ──" if nuevo_modo == MODO_SPACES else "── Aplicaciones ──"
        self._item_header_contextos.title = label

        # Limpiar items de contextos anteriores
        for item in self._items_contextos.values():
            try:
                del self.menu[item.title]
            except Exception:
                pass
        self._items_contextos.clear()

        self._actualizar_todo()

    # ─────────────────────────────────────────
    # Preferencias
    # ─────────────────────────────────────────

    def _gestionar_apps_ignoradas(self, sender):
        """Muestra las apps conocidas y permite marcar cuales ignorar."""
        ignoradas = storage.cargar_apps_ignoradas()
        conocidas = self._timer.contextos_conocidos()

        if not conocidas:
            rumps.alert(
                title="Ignorar aplicaciones",
                message="Aun no hay aplicaciones registradas.\n"
                "Usa DeskTimer en modo Apps primero.",
                ok="Cerrar",
            )
            return

        # Mostrar apps de a una con opcion de ignorar/desigmorar
        for app in conocidas:
            estado = "IGNORADA" if app in ignoradas else "registrada"
            accion = "Dejar de ignorar" if app in ignoradas else "Ignorar"
            resp = rumps.alert(
                title=f"{app} — {estado}",
                message=f"¿Quieres {accion.lower()} '{app}'?",
                ok=accion,
                cancel="Siguiente",
            )
            if resp == 1:  # ok
                if app in ignoradas:
                    ignoradas.remove(app)
                else:
                    ignoradas.append(app)

        storage.guardar_apps_ignoradas(ignoradas)

    def _renombrar_spaces(self, sender):
        """Permite renombrar cada Space conocido."""
        nombres = storage.cargar_nombres_spaces()
        conocidos = (
            self._timer.contextos_conocidos()
            if self._timer.modo() == MODO_SPACES
            else []
        )

        if not conocidos:
            rumps.alert(
                title="Renombrar Spaces",
                message="Aun no hay Spaces registrados.\n"
                "Usa DeskTimer en modo Spaces primero.",
                ok="Cerrar",
            )
            return

        for sid in conocidos:
            nombre_actual = nombres.get(sid, f"Space {sid}")
            resp = rumps.Window(
                message=f"Nombre para Space {sid}:",
                title="Renombrar Space",
                default_text=nombre_actual,
                ok="Guardar",
                cancel="Cancelar",
                dimensions=(280, 24),
            ).run()

            if resp.clicked and resp.text.strip():
                nombres[sid] = resp.text.strip()

        storage.guardar_nombres_spaces(nombres)
        self._actualizar_seccion_contextos()

    def _config_pomodoro(self, sender):
        """Configura las duraciones del Pomodoro."""
        campos = [
            (
                "pomo_focus",
                "Duracion de enfoque (minutos)",
                self._settings["pomo_focus"] // 60,
            ),
            (
                "pomo_break",
                "Duracion de descanso (minutos)",
                self._settings["pomo_break"] // 60,
            ),
            (
                "pomo_long_break",
                "Duracion de descanso largo (minutos)",
                self._settings["pomo_long_break"] // 60,
            ),
            (
                "pomo_sessions_for_long",
                "Sesiones antes del descanso largo",
                self._settings["pomo_sessions_for_long"],
            ),
        ]

        for clave, label, actual in campos:
            resp = rumps.Window(
                message=f"{label}:",
                title="Configuracion Pomodoro",
                default_text=str(actual),
                ok="Guardar",
                cancel="Cancelar",
                dimensions=(280, 24),
            ).run()

            if resp.clicked:
                try:
                    val = int(resp.text.strip())
                    if clave in ("pomo_focus", "pomo_break", "pomo_long_break"):
                        self._settings[clave] = val * 60
                    else:
                        self._settings[clave] = val
                except ValueError:
                    pass

        storage.guardar_settings(self._settings)
        # Propagar settings actualizados al pomodoro
        self._pomo._settings = self._settings

    def _ver_historial(self, sender):
        """Muestra el historial de los ultimos 7 dias."""
        modo = self._timer.modo()
        if modo == MODO_SPACES:
            historial = storage.cargar_historial_spaces()
            nombres = storage.cargar_nombres_spaces()

            def nombre_ctx(k):
                return nombres.get(k, f"Space {k}")

        else:
            historial = storage.cargar_historial_apps()

            def nombre_ctx(k):
                return k

        hoy = datetime.date.today()
        lineas = []

        for i in range(7):
            dia = hoy - datetime.timedelta(days=i)
            dia_str = str(dia)
            label = (
                "Hoy" if i == 0 else ("Ayer" if i == 1 else dia.strftime("%a %d/%m"))
            )
            datos = historial.get(dia_str, {})

            if not datos:
                lineas.append(f"{label}: sin datos")
                continue

            lineas.append(f"── {label} ──")
            total = 0.0
            for k, secs in sorted(datos.items(), key=lambda x: x[1], reverse=True):
                lineas.append(f"  {nombre_ctx(k):<22} {fmt_tiempo(secs)}")
                total += secs
            lineas.append(f"  {'TOTAL':<22} {fmt_tiempo(total)}")
            lineas.append("")

        rumps.alert(
            title="Historial — Ultimos 7 dias",
            message="\n".join(lineas) if lineas else "Sin historial.",
            ok="Cerrar",
        )

    # ─────────────────────────────────────────
    # Salir
    # ─────────────────────────────────────────

    def _salir(self, sender):
        self._timer.guardar_todo()
        self._tareas.guardar()
        storage.guardar_settings(
            {
                **self._settings,
                "pomo_sesiones_hoy": self._pomo.datos_sesiones(),
            }
        )
        rumps.quit_application()

    def _signal_salir(self, signum, frame):
        self._timer.guardar_todo()
        self._tareas.guardar()
        raise SystemExit(0)
