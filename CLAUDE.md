# DeskTimer macOS — Contexto del proyecto

App de barra de menú para macOS que rastrea tiempo por Space (escritorio virtual) y gestiona tareas por escritorio.

## Cómo ejecutar

```bash
cd "/Users/camilo/Scripts Tools/carpeta sin título/DeskTimer/desk-timer-macos"
python3 desktimer.py
```

### Dependencias
```bash
pip3 install rumps pyobjc-framework-Cocoa pyobjc-framework-Quartz
```

---

## Arquitectura

**Un solo proceso, dos status items en la barra de menú:**

| Item | Controla | Título |
|------|----------|--------|
| Timer | rumps | `EspacioNombre  H:MM:SS` |
| Tasks | NSStatusItem nativo (AppKit) | Nombre de tarea fijada o `Tasks` |

### Archivo principal
`desktimer.py` — ~1300 líneas. Todo en un solo archivo.

### Datos persistidos en `~/.desktimer/`
| Archivo | Contenido |
|---------|-----------|
| `space_history.json` | `{fecha: {sid: segundos}}` |
| `space_names.json` | `{sid: "Nombre personalizado"}` |
| `space_numbers.json` | `{sid: n}` → mapea CGS ID → número secuencial |
| `deleted_spaces.json` | `[sid, ...]` → espacios eliminados que no deben reaparecer |
| `space_goals.json` | `{sid: segundos_meta_diaria}` |
| `space_tasks.json` | `{sid: [task_dict, ...]}` |
| `pinned.json` | `{sid: [task_id, task_id2, ...]}` lista ordenada |
| `settings.json` | Configuración de pomodoro e idle |

---

## Clases principales

### `SpaceObserver(NSObject)`
Recibe notificaciones de macOS cuando cambia el Space activo o el sistema despierta del sleep.

### `TaskRowView(NSView)` — frame 280×22
Fila de tarea embebida en NSMenuItem. Sin emojis.
- Pin button (`·` / `1` / `2`...) → `onPin:`
- Check button (`○` / `✓`) → `onToggle:`
- NSTextField no-editable (texto de la tarea)
- Delete button (`✕`) → `onDelete:`
- `acceptsFirstMouse_` → True (necesario para eventos en menú)

> **CRÍTICO:** Los selectores de acción deben ser `"onPin:"`, `"onToggle:"`, `"onDelete:"` — NO `"onPin_:"`. En PyObjC, el método `onPin_` responde al selector `onPin:`.

### `AddTaskView(NSView)` — frame 280×26
Campo inline para agregar tareas. Solo Enter confirma (sin delegate de blur para evitar reentrancia).
- `acceptsFirstMouse_` → True
- `mouseDown_` → llama `makeFirstResponder_` en el text field
- `cell().setScrollable_(True)` + `setWraps_(False)` para campo de una línea
- Acción `"onAdd:"` dispara `onAdd_`

> **IMPORTANTE:** No usar `AddTaskFieldDelegate` (controlTextDidEndEditing_). AppKit lo destruye con referencia débil durante el rebuild del menú, causando llamadas reentrantes o crashes silenciosos.

### `DeskTimerApp(rumps.App)`
App principal. Métodos clave:

**Timer:**
- `_tick()` — cada 1s: actualiza título, espacio actual, pomodoro, tooltips, título de tasks SI
- `_get_live_seconds_unlocked(space_id)` — llamar con `_lock` adquirido
- `handle_space_change()` — acumula tiempo del espacio anterior, cambia al nuevo
- `_check_idle()` — pausa automática si idle > umbral

**Menú timer (rumps):**
- `_build_menu()` — construye menú con solo el espacio activo (no lista todos)
- `_update_space_items()` — actualiza solo el ítem del espacio actual via `item._menuitem.setTitle_()` (no `item.title =` para no romper el índice interno de rumps)
- `_delete_space_action()` — usa `self.menu._menu.removeItem_(item._menuitem)` (no `del self.menu[title]` porque el título cambia cada segundo)

**Menú tareas (NSMenu nativo):**
- `_build_tasks_status_item()` — crea el segundo NSStatusItem
- `_rebuild_tasks_ns_menu()` — reconstruye todo el NSMenu de tareas desde cero
- `_focus_add_field()` — después de agregar tarea, busca el AddTaskView y llama `makeFirstResponder_`
- `_update_tasks_si_title()` — actualiza el título del tasks status item

**Tareas:**
- `add_task_from_text(text)` → guarda tarea → rebuild → `_focus_add_field()`
- `toggle_task_done(task_id)` → si tarea fijada se completa, se elimina de la lista de pins (auto-avance)
- `toggle_task_pin(task_id)` → añade/quita de `_pinned[sid]` (lista ordenada)
- `delete_task(task_id)`
- `rename_task(task_id, new_text)`

**Espacios:**
- `_get_ordered_space_ids()` — usa `CGSCopyManagedDisplaySpaces` vía `objc.loadBundleFunctions` para obtener IDs en orden visual real
- `_init_space_numbers()` — al inicio asigna "Escritorio 1", "Escritorio 2"... en orden visual
- `_next_space_name(sid)` — asigna siguiente número secuencial
- IDs de CGS son grandes (ej. 316, 318, 297) — no corresponden al número visual del escritorio

---

## Bugs conocidos y decisiones de diseño

### Por qué `item._menuitem.setTitle_()` en vez de `item.title =`
rumps almacena ítems por título en un dict interno. Si cambias `item.title` cada segundo (el timer lo haría), la clave del dict queda desincronizada. Para borrar luego con `del self.menu[title]` fallaría. Solución: actualizar el NSMenuItem directamente y borrar con `removeItem_()`.

### Por qué los espacios eliminados persisten
`CGSGetActiveSpace` devuelve el ID del espacio activo. Si el usuario elimina un espacio del tracker pero luego lo visita, `handle_space_change` lo re-añadiría. Solución: `_deleted_sids` set persistido en `deleted_spaces.json`. Se chequea antes de re-añadir.

### Por qué el campo de agregar tarea necesita `acceptsFirstMouse_`
En menús de status bar de macOS, los custom NSViews no reciben eventos de ratón a menos que `acceptsFirstMouse_` devuelva True. Especialmente crítico cuando el menú solo tiene el campo (sin filas de tarea que "inicialicen" el routing de eventos).

### Por qué no hay confirmación al eliminar escritorio
`rumps.alert` usa `NSAlert.runModal()` que bloquea el run loop → el timer se pausa → parece que la app se congela. Se eliminó el diálogo de confirmación.

### Por qué `_pinned` es lista y no single id
Permite fijar múltiples tareas en orden. El title del Tasks status item muestra la primera no-completada. Al completar una tarea fijada se elimina de la lista automáticamente (la siguiente sube a [1]).

---

## Estado actual (abril 2026)

**Funciona:**
- Timer por Space con detección automática
- Dos status items separados (timer + tasks)
- Nombres de escritorios secuenciales en orden visual real
- Eliminar escritorio (no reaparece al volver)
- Renombrar escritorio / meta diaria (submenú por espacio)
- Pausa automática por idle
- Pomodoro (enfoque / descanso / descanso largo)
- Tareas por escritorio: agregar (Enter), completar, pin ordenado, eliminar
- Re-foco automático en el campo de tareas tras agregar
- Menú tareas se cierra al hacer click afuera
- Pins múltiples ordenados, auto-avance al completar

**Pendiente / mejoras posibles:**
- Editar nombre de tarea inline (actualmente el text field de la fila es no-editable)
- Historial por día (los datos se guardan pero no hay vista de historial)
- Exportar datos
- Notificaciones al alcanzar meta de tiempo
