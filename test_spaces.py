#!/usr/bin/env python3
# test_spaces.py — Verificar que las APIs de macOS funcionan antes de integrar en desktimer.py
# Instrucciones: ejecutar, cambiar de Space manualmente (ctrl+flechas o swipe), verificar prints

import ctypes
import ctypes.util
import time
import sys

# ─── 1. CoreGraphics privado ───────────────────────────────────────────────────

def init_cgs():
    cg = ctypes.cdll.LoadLibrary(ctypes.util.find_library("CoreGraphics"))

    # CGSMainConnectionID() no requiere argumentos y es más fiable que
    # CGSGetDefaultConnectionForProcess en macOS reciente
    cg.CGSMainConnectionID.restype = ctypes.c_int
    cg.CGSMainConnectionID.argtypes = []

    cg.CGSGetActiveSpace.restype = ctypes.c_uint64
    cg.CGSGetActiveSpace.argtypes = [ctypes.c_int]

    return cg

def get_current_space_id(cg):
    conn = cg.CGSMainConnectionID()
    return cg.CGSGetActiveSpace(conn)

def test_space_id():
    print("─── Test 1: CGSGetActiveSpace ───")
    try:
        cg = init_cgs()
        space_id = get_current_space_id(cg)
        print(f"  Space actual: {space_id}")
        assert isinstance(space_id, int) and space_id > 0, "Space ID inválido"
        print("  ✓ OK\n")
        return cg
    except Exception as e:
        print(f"  ✗ ERROR: {e}\n")
        return None

# ─── 2. Observer de NSWorkspace ────────────────────────────────────────────────

def test_space_observer(cg, duration=15):
    print("─── Test 2: NSWorkspaceActiveSpaceDidChangeNotification ───")
    print(f"  Cambia de Space en los próximos {duration} segundos para verificar...")

    try:
        from AppKit import NSWorkspace
        from Foundation import NSObject, NSRunLoop, NSDate

        class TestObserver(NSObject):
            def init(self):
                self = super().init()
                self.count = 0
                return self

            def spaceDidChange_(self, notification):
                self.count += 1
                sid = get_current_space_id(cg) if cg else "?"
                print(f"  → cambio #{self.count}: Space ID = {sid}")

        observer = TestObserver.alloc().init()
        ws = NSWorkspace.sharedWorkspace()
        nc = ws.notificationCenter()
        nc.addObserver_selector_name_object_(
            observer,
            "spaceDidChange:",
            "NSWorkspaceActiveSpaceDidChangeNotification",
            None,
        )

        # Bombear el run loop manualmente durante N segundos
        deadline = time.time() + duration
        while time.time() < deadline:
            NSRunLoop.currentRunLoop().runUntilDate_(
                NSDate.dateWithTimeIntervalSinceNow_(0.1)
            )

        nc.removeObserver_(observer)

        if observer.count > 0:
            print(f"  ✓ OK — {observer.count} cambio(s) detectado(s)\n")
        else:
            print("  ⚠ No se detectaron cambios (¿cambiaste de Space?)\n")

    except Exception as e:
        print(f"  ✗ ERROR: {e}\n")

# ─── 3. Idle time ──────────────────────────────────────────────────────────────

def test_idle():
    print("─── Test 3: CGEventSourceSecondsSinceLastEventType ───")
    try:
        import Quartz

        idle = Quartz.CGEventSourceSecondsSinceLastEventType(
            Quartz.kCGEventSourceStateCombinedSessionState,
            Quartz.kCGAnyInputEventType,
        )
        print(f"  Segundos de inactividad: {idle:.1f}")
        assert idle >= 0, "Valor negativo"
        print("  ✓ OK\n")
    except Exception as e:
        print(f"  ✗ ERROR: {e}\n")

# ─── 4. Sonido del sistema ─────────────────────────────────────────────────────

def test_sound():
    print("─── Test 4: NSSound ───")
    try:
        from AppKit import NSSound
        import time

        sound = NSSound.soundNamed_("Glass")
        if sound:
            sound.play()
            time.sleep(1)
            print("  ✓ OK — deberías haber escuchado 'Glass'\n")
        else:
            print("  ⚠ NSSound.soundNamed_('Glass') retornó None\n")
    except Exception as e:
        print(f"  ✗ ERROR: {e}\n")

# ─── Main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("\n=== DeskTimer — Test de APIs de macOS ===\n")

    cg = test_space_id()
    test_idle()
    test_sound()

    if "--skip-observer" not in sys.argv:
        test_space_observer(cg, duration=15)
    else:
        print("─── Test 2: observer omitido (--skip-observer) ───\n")

    print("=== Fin de tests ===\n")
