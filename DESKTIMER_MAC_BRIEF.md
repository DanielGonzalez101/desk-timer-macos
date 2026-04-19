# DeskTimer para macOS — Brief técnico para Claude Code

> Este documento es una guía completa para construir **DeskTimer macOS**, una app de barra de menú (menu bar) que cronometra automáticamente el tiempo en cada Space (escritorio virtual) de macOS. Léelo completo antes de escribir una sola línea de código.

---

## 1. Qué es DeskTimer

DeskTimer es un **tracker pasivo de tiempo por escritorio virtual**. El usuario asigna nombres a sus Spaces de macOS (Trabajo, Estudio, Personal...) y la app registra automáticamente cuánto tiempo pasa en cada uno. No requiere que el usuario inicie ni detenga timers manualmente — solo cambia de Space como lo haría normalmente.

Esta versión para macOS incluye tres módulos:

1. **Timer pasivo por Space** (core)
2. **Pomodoro integrado**
3. **To-Do list por Space**

---

## 2. Stack tecnológico

| Componente | Tecnología |
|-----------|-----------|
| Lenguaje | **Python 3.10+** |
| Menu bar | **rumps** (`pip install rumps`) — framework para menu bar apps en macOS |
| Detección de Spaces | **PyObjC** (`pip install pyobjc-framework-Cocoa`) — bridge Python → Objective-C |
| Notificaciones de cambio de Space | `NSWorkspace.sharedWorkspace().notificationCenter()` con `NSWorkspaceActiveSpaceDidChangeNotification` |
| Identificación del Space actual | API privada `CGSGetActiveSpace` vía `ctypes` sobre el framework Quartz/CoreGraphics |
| Sonido Pomodoro | `NSSound` de AppKit (vía PyObjC) o `afplay` como fallback |
| Persistencia | Archivos JSON en `~/.desktimer/` |
| Detección de inactividad | `CGEventSourceSecondsSinceLastEventType` de Quartz |

### Dependencias a instalar

```bash
pip install rumps pyobjc-framework-Cocoa pyobjc-framework-Quartz
```

No se necesitan más dependencias. Todo lo demás es librería estándar de Python.

---

## 3. Arquitectura de la app

### 3.1 Estructura general

La app corre como un **ícono en la barra de menú** de macOS (junto a WiFi, batería, reloj, etc.). Al hacer click en el ícono se despliega un menú con:

- Nombre del Space actual + tiempo acumulado (título del menú, se actualiza cada segundo)
- Separador
- Sección de historial: lista de todos los Spaces con nombre y tiempo
- Separador
- Sección Pomodoro: estado actual, iniciar/pausar, saltar fase
- Separador
- Sección To-Do: tareas del Space actual
- Separador
- Pausa global
- Preferencias (renombrar Spaces, metas)
- Salir

### 3.2 Título del menu bar (siempre visible)

El ícono/título que se muestra permanentemente en la barra de menú debe ser compacto:

**Formato**: `⏱ NombreSpace H:MM:SS`

Ejemplos:
- `⏱ Trabajo 2:34:15`
- `⏱ Estudio 0:45:02`
- `⏱ ⏸ Pausado` (cuando está en pausa global)
- `⏱ 🍅 Trabajo 2:34:15` (cuando Pomodoro está activo)

El título se actualiza cada **1 segundo** usando `rumps.Timer`.

### 3.3 Threads

| Thread | Propósito |
|--------|-----------|
| **Principal** | Rumps app (incluye el run loop de Cocoa necesario para recibir notificaciones del sistema) |
| **Autosave (daemon)** | Guarda los JSON cada 30 segundos |
| **Idle watcher (daemon)** | Cada 10 s verifica inactividad del usuario |

> **IMPORTANTE**: rumps ya maneja un NSRunLoop internamente, por lo que las notificaciones de NSWorkspace funcionan correctamente dentro del thread principal. No necesitas crear un thread separado para el message loop como en Windows.

### 3.4 Sincronización

Usar `threading.Lock` para proteger:
- `_lock` → `_current_space`, `_space_seconds`, `_session_start`, flags de pausa
- `_pomo_lock` → estado del Pomodoro
- `_task_lock` → diccionario de tareas

---

## 4. Detección de Spaces en macOS

### 4.1 Detectar cambio de Space (evento)

Usar `NSWorkspace` + `NSNotificationCenter` de PyObjC:

```python
from AppKit import NSWorkspace, NSObject
from Foundation import NSObject as FoundationNSObject

# Registrar observer para cambios de Space
workspace = NSWorkspace.sharedWorkspace()
notification_center = workspace.notificationCenter()

# El nombre de la notificación es:
# NSWorkspaceActiveSpaceDidChangeNotification
# Se dispara cada vez que el usuario cambia de Space (swipe, ctrl+flechas, etc.)

notification_center.addObserver_selector_name_object_(
    observer_instance,           # instancia de un NSObject subclass
    "spaceDidChange:",           # selector (método que se ejecuta)
    "NSWorkspaceActiveSpaceDidChangeNotification",
    None
)
```

El `observer_instance` debe ser una subclase de `NSObject` con el método:

```python
class SpaceObserver(NSObject):
    def spaceDidChange_(self, notification):
        # Aquí llamas a tu función de switch
        new_space_id = get_current_space_id()
        app.handle_space_change(new_space_id)
```

### 4.2 Obtener el Space actual (ID)

macOS no expone un API público para obtener el número de Space. Hay que usar la API privada de CoreGraphics:

```python
import ctypes
import ctypes.util

# Cargar CoreGraphics
cg = ctypes.cdll.LoadLibrary(ctypes.util.find_library("CoreGraphics"))

# Funciones necesarias
# CGSGetActiveSpace necesita la conexión por defecto
_CGSDefaultConnection = cg.CGSGetDefaultConnectionForProcess
_CGSDefaultConnection.restype = ctypes.c_int

CGSGetActiveSpace = cg.CGSGetActiveSpace
CGSGetActiveSpace.argtypes = [ctypes.c_int]
CGSGetActiveSpace.restype = ctypes.c_uint64

def get_current_space_id():
    conn = _CGSDefaultConnection()
    space_id = CGSGetActiveSpace(conn)
    return space_id
```

> **NOTA**: `CGSGetActiveSpace` retorna un ID numérico único por Space (ej: 1, 5, 12, 27...), **NO** un índice secuencial (0, 1, 2...). Los IDs pueden ser números grandes y no consecutivos. El sistema de persistencia debe usar estos IDs como keys en los diccionarios.

### 4.3 Obtener lista de todos los Spaces

Para saber cuántos Spaces existen y listarlos:

```python
import Quartz

# CGSCopySpaces puede obtener todos los spaces
CGSCopySpaces = cg.CGSCopySpaces
CGSCopySpaces.argtypes = [ctypes.c_int, ctypes.c_int]
CGSCopySpaces.restype = ctypes.c_void_p

# Masks:
# kCGSSpaceAll = 7 (incluye todos)
# kCGSSpaceUser = 1 (solo spaces del usuario, no fullscreen apps)
# kCGSSpaceCurrent = 5

# Alternativa más fiable: leer desde las preferencias del sistema
import subprocess
import plistlib

def get_space_ids():
    """Obtiene la lista de Space IDs desde las preferencias de Spaces"""
    result = subprocess.run(
        ["defaults", "read", "com.apple.spaces", "SpacesDisplayConfiguration"],
        capture_output=True, text=True
    )
    # Parsear la salida para extraer los Space IDs
    # Los IDs están en Management Data > Monitors > Spaces
    pass
```

> **ESTRATEGIA RECOMENDADA**: No intentes listar los Spaces al inicio. En vez de eso, mantén un diccionario que se va llenando conforme el usuario visita cada Space. Cada vez que `spaceDidChange_` se dispara, si el Space ID no existe en el diccionario, se agrega automáticamente. Esto evita depender de APIs privadas adicionales para el listado.

### 4.4 Fallback por polling

Si la notificación `NSWorkspaceActiveSpaceDidChangeNotification` no funciona de forma fiable en alguna versión de macOS, implementa un **fallback por polling** cada 1 segundo que compare el Space actual con el almacenado. Este polling se puede hacer en el mismo timer de actualización del título.

---

## 5. Detección de inactividad (idle)

Usar `Quartz.CGEventSourceSecondsSinceLastEventType`:

```python
import Quartz

def get_idle_seconds():
    """Retorna los segundos de inactividad del usuario"""
    idle = Quartz.CGEventSourceSecondsSinceLastEventType(
        Quartz.kCGEventSourceStateCombinedSessionState,
        Quartz.kCGAnyInputEventType
    )
    return idle
```

### Umbrales

| Umbral | Valor | Acción |
|--------|-------|--------|
| `IDLE_THRESHOLD` | 200 segundos | Pausar automáticamente |
| `IDLE_RESUME_AT` | 30 segundos | Reanudar (idle < 30s = usuario volvió) |

### Comportamiento

- Si `get_idle_seconds() >= 200` → activar pausa automática. **Descontar** esos 200 segundos del tiempo acumulado del Space actual (porque el usuario ya no estaba).
- Si estaba en pausa automática y `get_idle_seconds() < 30` → reanudar.
- La pausa automática es distinta de la pausa manual. Si el usuario pausó manualmente, la detección de idle NO debe reanudar.

### Detección de suspensión (sleep/wake)

Registrar también la notificación de wake del sistema:

```python
# NSWorkspaceDidWakeNotification — cuando la Mac despierta de sleep
notification_center.addObserver_selector_name_object_(
    observer_instance,
    "systemDidWake:",
    "NSWorkspaceDidWakeNotification",
    None
)
```

Al despertar, verificar si el día cambió (`reset_today()` si aplica) y recalcular el estado.

---

## 6. Persistencia (JSON)

### 6.1 Directorio de datos

Todos los archivos van en `~/.desktimer/`:

```
~/.desktimer/
├── space_history.json
├── space_names.json
├── space_goals.json
├── space_tasks.json
├── settings.json
└── sounds/           # (opcional) .wav/.aiff para Pomodoro
```

Crear el directorio al iniciar si no existe.

### 6.2 Esquema de cada archivo

#### `space_history.json`
```json
{
  "2026-04-18": {
    "1": 5432.5,
    "5": 1234.0,
    "12": 789.5
  },
  "2026-04-17": {
    "1": 14400.0,
    "5": 3600.0
  }
}
```
- Keys de primer nivel: fecha `YYYY-MM-DD`
- Keys de segundo nivel: Space ID (string del número que retorna `CGSGetActiveSpace`)
- Valores: segundos acumulados (float)

#### `space_names.json`
```json
{
  "1": "Trabajo",
  "5": "Estudio",
  "12": "Personal"
}
```

#### `space_goals.json`
```json
{
  "1": 14400,
  "5": 7200
}
```
- Valores en segundos. `14400` = 4 horas.

#### `space_tasks.json`
```json
{
  "1": [
    {
      "id": "a1b2c3",
      "text": "Revisar propuesta cliente",
      "done": false,
      "pinned": true,
      "date_done": null
    }
  ],
  "5": []
}
```
- `id`: string aleatorio (usar `uuid.uuid4().hex[:6]`)
- `date_done`: `"YYYY-MM-DD"` o `null`

#### `settings.json`
```json
{
  "pomo_focus": 1500,
  "pomo_break": 300,
  "pomo_long_break": 900,
  "pomo_sessions_for_long": 4,
  "idle_threshold": 200,
  "idle_resume": 30
}
```

### 6.3 Autosave

Un thread daemon guarda todos los JSON cada 30 segundos. También se guarda al salir la app (hook en `rumps` con `@rumps.quit_application`).

---

## 7. Estructura del menú (rumps)

### 7.1 Menú completo

```
⏱ Trabajo 2:34:15              ← título del menu bar (se actualiza cada 1s)
┌─────────────────────────────┐
│ ── Espacios ──              │  ← sección header (disabled)
│ ● Trabajo      2:34:15     │  ← Space actual (marcado con ●)
│   Estudio      0:45:02     │
│   Personal     0:12:30     │
│ ─────────────────────────── │
│ ── Pomodoro ──              │
│ 🍅 Enfoque: 18:32          │  ← estado actual (disabled, solo info)
│ ▶ Iniciar                   │  ← o "⏸ Pausar" si activo
│ ⏭ Saltar fase               │
│ Sesiones hoy: 3             │  ← contador (disabled)
│ ─────────────────────────── │
│ ── Tareas (Trabajo) ──      │  ← cambia según Space actual
│ ☐ Revisar propuesta         │
│ ☐ Enviar reporte            │
│ ☑ Llamar a cliente          │  ← tachada visualmente
│ + Agregar tarea...          │
│ ─────────────────────────── │
│ ⏸ Pausar timer              │  ← o "▶ Reanudar"
│ ─────────────────────────── │
│ Renombrar Spaces...         │  ← abre ventana
│ Configurar metas...         │  ← abre ventana
│ Configuración...            │  ← abre ventana de settings
│ ─────────────────────────── │
│ Salir                       │
└─────────────────────────────┘
```

### 7.2 Interacciones del menú

| Acción del usuario | Qué hace |
|-------------------|----------|
| Click en un Space de la lista | No hace nada (solo informativo). Opcionalmente: podría pausar/despausar ese Space individual |
| Click en "Iniciar/Pausar" Pomodoro | Alterna el estado del Pomodoro |
| Click en "Saltar fase" | Avanza a la siguiente fase del Pomodoro |
| Click en una tarea `☐` | La marca como `done` (cambia a `☑`) |
| Click en una tarea `☑` | La desmarca (vuelve a `☐`) |
| Click en "+ Agregar tarea..." | Muestra un `rumps.Window` (input dialog) para escribir la tarea |
| Click en "Pausar timer" | Pausa global — congela todo el conteo |
| Click en "Renombrar Spaces..." | Abre una ventana (`rumps.Window`) por cada Space para editar nombre |
| Click en "Configurar metas..." | Abre ventana para definir meta en horas por Space |
| Click en "Configuración..." | Abre ventana para editar duraciones del Pomodoro y umbrales de idle |
| Click en "Salir" | Guarda todo y cierra |

### 7.3 Actualización dinámica del menú

`rumps` permite actualizar items del menú dinámicamente. Usar un `rumps.Timer` de 1 segundo para:

1. Actualizar el título de la barra de menú (`app.title = "⏱ Trabajo 2:34:15"`)
2. Actualizar los tiempos de cada Space en el menú
3. Actualizar el estado del Pomodoro
4. Verificar si cambió el día (reset automático)

**NOTA sobre rumps y actualización de menú**: `rumps` no permite reconstruir el menú completo fácilmente en cada tick. La estrategia es:

- Los items de tiempo de cada Space se actualizan cambiando su `title` property.
- Los items del Pomodoro se actualizan igual.
- Las tareas se reconstruyen solo cuando cambian (agregar/completar/cambiar de Space).

---

## 8. Timer pasivo por Space

### 8.1 Variables de estado

```python
_current_space = None          # Space ID actual (int)
_space_seconds = {}            # {space_id_str: float} — segundos acumulados hoy
_session_start = time.time()   # timestamp del inicio de la sesión actual
_is_paused = False             # pausa global manual
_is_idle_paused = False        # pausa por inactividad
_today = datetime.date.today() # para detectar cambio de día
```

### 8.2 Flujo de cambio de Space

Cuando se recibe `NSWorkspaceActiveSpaceDidChangeNotification`:

1. Obtener `new_space_id = get_current_space_id()`
2. Si `new_space_id == _current_space` → ignorar (puede pasar con fullscreen apps)
3. Con `_lock`:
   a. Si no está pausado: calcular `elapsed = time.time() - _session_start` y sumarlo a `_space_seconds[str(_current_space)]`
   b. Actualizar `_current_space = new_space_id`
   c. Resetear `_session_start = time.time()`
   d. Si el Space es nuevo (no existe en `_space_seconds`): inicializarlo en 0
4. Actualizar el menú (marcar el nuevo Space con ●, actualizar título)

### 8.3 Cálculo de tiempo en vivo

Para mostrar el tiempo actual sin escribir al estado:

```python
def get_live_seconds(space_id):
    base = _space_seconds.get(str(space_id), 0)
    if space_id == _current_space and not _is_paused and not _is_idle_paused:
        base += time.time() - _session_start
    return base
```

### 8.4 Reset al cambiar de día

En cada tick del timer de 1 segundo:

```python
today = datetime.date.today()
if today != _today:
    # Guardar el día anterior
    save_history()
    # Reset
    _space_seconds = {}
    _session_start = time.time()
    _today = today
    # Limpiar tareas completadas de ayer
    clean_old_done_tasks()
    # Reset sesiones Pomodoro
    _pomo_sessions = 0
```

### 8.5 Formato de tiempo

```python
def fmt_time(seconds):
    """Formatea segundos como H:MM:SS"""
    h = int(seconds) // 3600
    m = (int(seconds) % 3600) // 60
    s = int(seconds) % 60
    return f"{h}:{m:02d}:{s:02d}"
```

---

## 9. Pomodoro

### 9.1 Fases

```
focus (25 min) → break (5 min) → focus → break → focus → break → focus → long_break (15 min) → repeat
```

Cada N sesiones de focus completadas → `long_break` en vez de `break`. N es configurable (default 4).

### 9.2 Variables de estado

```python
_pomo_active = False
_pomo_paused = False
_pomo_phase = "focus"          # "focus", "break", "long_break"
_pomo_remaining = 1500         # segundos restantes de la fase actual
_pomo_last_tick = None         # timestamp del último tick
_pomo_sessions = 0             # sesiones focus completadas hoy
_pomo_sessions_date = "2026-04-18"  # para resetear al cambiar de día
```

### 9.3 Tick del Pomodoro

En cada tick de 1 segundo del timer principal:

```python
if _pomo_active and not _pomo_paused and not _is_paused:
    now = time.time()
    if _pomo_last_tick:
        elapsed = now - _pomo_last_tick
        _pomo_remaining -= elapsed
    _pomo_last_tick = now

    if _pomo_remaining <= 0:
        complete_pomo_phase()
```

### 9.4 Completar fase

```python
def complete_pomo_phase():
    play_sound()  # sonido de transición
    send_notification()  # notificación nativa de macOS

    if _pomo_phase == "focus":
        _pomo_sessions += 1
        if _pomo_sessions % _settings["pomo_sessions_for_long"] == 0:
            _pomo_phase = "long_break"
            _pomo_remaining = _settings["pomo_long_break"]
        else:
            _pomo_phase = "break"
            _pomo_remaining = _settings["pomo_break"]
    else:  # break o long_break
        _pomo_phase = "focus"
        _pomo_remaining = _settings["pomo_focus"]

    _pomo_last_tick = time.time()
```

### 9.5 Notificaciones nativas macOS

Usar `rumps.notification()` para enviar notificaciones del sistema:

```python
rumps.notification(
    title="DeskTimer",
    subtitle="Pomodoro",
    message="¡Tiempo de descanso! 5 minutos",
    sound=True  # reproduce el sonido por defecto del sistema
)
```

### 9.6 Integración con pausa global

Si el usuario pausa el timer global:
- El Pomodoro también se pausa
- Se guarda `_pomo_was_active` para recordar si estaba corriendo
- Al reanudar: si `_pomo_was_active`, reanudar también el Pomodoro

### 9.7 Mostrar en menú

El item del Pomodoro en el menú debe mostrar:

- Fase actual + tiempo restante: `🍅 Enfoque: 18:32` o `☕ Descanso: 3:45` o `🌴 Descanso largo: 12:00`
- Botón de acción: `▶ Iniciar` / `⏸ Pausar` / `▶ Reanudar`
- `⏭ Saltar fase`
- `Sesiones hoy: 3` (informativo)

---

## 10. To-Do list por Space

### 10.1 Estructura de una tarea

```python
{
    "id": "a1b2c3",       # uuid corto
    "text": "Descripción", # texto de la tarea
    "done": False,         # completada
    "pinned": False,       # fijada (no se usa en v1 Mac, reservado)
    "date_done": None      # "YYYY-MM-DD" cuando se completó
}
```

### 10.2 Menú de tareas

Las tareas se muestran en el menú bajo el header `── Tareas (NombreSpace) ──`:

- Las tareas pendientes aparecen como `☐ texto`
- Las completadas como `☑ texto`
- Click alterna el estado
- Al final: `+ Agregar tarea...` que abre un `rumps.Window` con campo de texto

### 10.3 Orden

1. Tareas pendientes (orden de creación)
2. Tareas completadas (al final)

### 10.4 Limpieza automática

Al iniciar la app o al cambiar de día:
- Eliminar tareas con `done == True` y `date_done < hoy`

### 10.5 Cambio de Space → actualizar tareas

Cuando el usuario cambia de Space, la sección de tareas en el menú debe actualizarse para mostrar las tareas del nuevo Space. Esto requiere reconstruir esa sección del menú.

---

## 11. Metas diarias

### 11.1 Comportamiento

- Cada Space puede tener una meta diaria en segundos.
- En el menú, el Space con meta muestra progreso: `● Trabajo 2:34:15 / 4:00:00 (64%)`
- La meta se configura desde "Configurar metas..." usando `rumps.Window` con presets.

### 11.2 Sin barra visual en el menú

Los menús nativos de macOS no soportan barras de progreso. En su lugar, usar indicadores textuales:

- `< 50%` → `🔴` prefijo
- `50-90%` → `🟡` prefijo
- `≥ 90%` → `🟢` prefijo
- `≥ 100%` → `✅` prefijo

Ejemplo en el menú:
```
🟡 Trabajo      2:34:15 / 4:00:00
🟢 Estudio      1:45:00 / 2:00:00
  Personal     0:12:30
```

---

## 12. Sonido

### 12.1 Sonido del Pomodoro

Para los sonidos de transición del Pomodoro, usar `NSSound` de AppKit:

```python
from AppKit import NSSound

def play_sound(filename=None):
    if filename:
        sound = NSSound.alloc().initWithContentsOfFile_byReference_(filename, True)
    else:
        # Sonido del sistema como fallback
        sound = NSSound.soundNamed_("Glass")  # o "Ping", "Pop", "Purr"
    if sound:
        sound.play()
```

### 12.2 Sonidos por fase

| Evento | Sonido |
|--------|--------|
| Inicio de enfoque | `Glass` (o archivo custom `focus.aiff`) |
| Inicio de descanso | `Purr` (o archivo custom `break.aiff`) |
| Inicio de descanso largo | `Hero` (o archivo custom `long_break.aiff`) |

---

## 13. Estructura del proyecto

```
DeskTimer-Mac/
├── desktimer.py              # App principal (monolito)
├── requirements.txt          # rumps, pyobjc-framework-Cocoa, pyobjc-framework-Quartz
├── README.md                 # Documentación de usuario
├── setup.py                  # (opcional) para empaquetar con py2app
└── sounds/                   # (opcional) sonidos custom .aiff
    ├── focus.aiff
    ├── break.aiff
    └── long_break.aiff
```

### `requirements.txt`
```
rumps>=0.4.0
pyobjc-framework-Cocoa>=9.0
pyobjc-framework-Quartz>=9.0
```

---

## 14. Clase principal — Esqueleto

```python
import rumps
import time
import datetime
import json
import os
import threading
import ctypes
import ctypes.util
import uuid

from AppKit import NSWorkspace, NSObject, NSSound
from Foundation import NSObject as FoundationNSObject
import Quartz

# ─── Constantes ───
DATA_DIR = os.path.expanduser("~/.desktimer")
IDLE_THRESHOLD = 200
IDLE_RESUME_AT = 30
TICK_INTERVAL = 1  # segundos
AUTOSAVE_INTERVAL = 30  # segundos

# ─── CoreGraphics privado ───
# (implementar get_current_space_id() aquí)

# ─── Observer de Spaces ───
class SpaceObserver(NSObject):
    app = None  # referencia a DeskTimerApp

    def spaceDidChange_(self, notification):
        if self.app:
            self.app.handle_space_change()

# ─── App principal ───
class DeskTimerApp(rumps.App):
    def __init__(self):
        super().__init__("⏱ Cargando...", quit_button="Salir")

        # Estado del timer
        self._lock = threading.Lock()
        self._current_space = None
        self._space_seconds = {}
        self._session_start = time.time()
        self._is_paused = False
        self._is_idle_paused = False
        self._today = datetime.date.today()

        # Estado Pomodoro
        self._pomo_lock = threading.Lock()
        self._pomo_active = False
        # ... (resto de variables pomo)

        # Estado Tareas
        self._task_lock = threading.Lock()
        self._tasks = {}

        # Cargar datos
        self._load_all()

        # Detectar Space inicial
        self._current_space = get_current_space_id()

        # Registrar observer
        self._setup_space_observer()

        # Construir menú
        self._build_menu()

        # Timer de actualización (1 segundo)
        self._timer = rumps.Timer(self._tick, TICK_INTERVAL)
        self._timer.start()

        # Thread de autosave
        self._start_autosave()

        # Thread de idle watcher
        self._start_idle_watcher()

    # ... implementar todos los métodos ...

if __name__ == "__main__":
    app = DeskTimerApp()
    app.run()
```

> **IMPORTANTE**: Este es solo un esqueleto para guiar la estructura. Claude Code debe implementar cada método completo con toda la lógica.

---

## 15. Casos borde a manejar

| Caso | Solución |
|------|----------|
| La app inicia y ya hay historial del día actual | Cargar `_space_seconds` desde `space_history.json[hoy]` y continuar sumando |
| El usuario tiene un solo Space | Funciona normal — solo un Space en la lista |
| Space ID cambia tras reinicio del Mac | Los Space IDs de macOS son persistentes durante la sesión pero pueden cambiar tras reinicio. Si un ID nuevo aparece y no tiene nombre, asignarle "Espacio N" por defecto |
| Fullscreen apps crean Spaces nuevos | macOS crea Spaces temporales para apps en fullscreen. Estos generan cambios de Space. Tratarlos como cualquier otro Space |
| El Mac entra en sleep | Registrar `NSWorkspaceDidWakeNotification`. Al despertar: recalcular estado, verificar cambio de día |
| Salto de `time.time()` por sleep | Si `time.time() - _session_start > IDLE_THRESHOLD`, asumir que hubo sleep y descontar el exceso |
| El usuario cierra la app sin usar "Salir" | Registrar handler de señales (`signal.SIGTERM`) para guardar datos |
| Error al cargar JSON corrupto | Try/except al cargar; si falla, iniciar con datos vacíos y loguear el error |
| Cambio de día a medianoche | El tick de 1s detecta `date.today() != _today` y hace reset |

---

## 16. Testing

Crear un script `test_spaces.py` que verifique:

1. Que `get_current_space_id()` retorna un entero válido
2. Que el observer de `NSWorkspaceActiveSpaceDidChangeNotification` funciona (mostrar prints al cambiar de Space)
3. Que `get_idle_seconds()` retorna valores correctos
4. Que los sonidos funcionan

```python
# test_spaces.py
# Ejecutar primero para verificar que las APIs funcionan
# Instrucciones: ejecutar, cambiar de Space manualmente, verificar que se imprimen los cambios
```

---

## 17. Orden de implementación sugerido

1. **`test_spaces.py`** — Verificar que las APIs de macOS funcionan (Space ID, notificaciones, idle)
2. **Core timer** — Menu bar app con rumps que muestra el Space actual y cronometra
3. **Persistencia** — Guardar/cargar JSON, autosave
4. **Renombrar Spaces** — Ventana para asignar nombres
5. **Historial completo** — Mostrar todos los Spaces con tiempos en el menú
6. **Pausa global** — Manual + automática por idle
7. **Detección de sleep/wake** — Manejar correctamente
8. **Reset de día** — Cambio automático a medianoche
9. **Pomodoro** — Fases, sonidos, notificaciones
10. **To-Do list** — Tareas por Space con toggle done/undone
11. **Metas diarias** — Configuración y visualización con indicadores de color
12. **Pulido** — Manejo de errores, logs, edge cases

---

## 18. Notas finales para Claude Code

- **Todo en un solo archivo `desktimer.py`** — monolito como la versión Windows. Solo el test va aparte.
- **Sin frameworks UI pesados** — solo rumps + menú nativo de macOS. Nada de tkinter, Qt, ni webviews.
- **Los comentarios del código en español**.
- **Probar cada API primero con el `test_spaces.py`** antes de integrar.
- **Si `CGSGetActiveSpace` no funciona**, intentar la alternativa de leer las preferencias del sistema con `defaults read com.apple.spaces`.
- **rumps maneja el NSRunLoop**, así que las notificaciones de NSWorkspace funcionan sin configuración extra.
- **Testar en macOS real** — estas APIs no funcionan en VMs normalmente.
- No usar emojis en logs/prints internos, solo en la UI del menú.
