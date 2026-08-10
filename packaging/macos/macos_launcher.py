#!/usr/bin/env python3
import atexit
import os
import sys
import threading
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
os.chdir(HERE)
sys.path.insert(0, str(HERE))

import webview
from werkzeug.serving import make_server
import app as wiredrive

# Force the native macOS application identity to the product name. Without
# this, Cocoa can inherit "Python" from the interpreter hosting pywebview.
try:
    from Foundation import NSProcessInfo
    NSProcessInfo.processInfo().setProcessName_("Wiredrive Sync")
except Exception:
    pass

HOST = "127.0.0.1"
http_server = None
server_thread = None


def backend_responding(url):
    try:
        with urllib.request.urlopen(url + "api/state", timeout=1.25) as response:
            return 200 <= getattr(response, "status", 200) < 500
    except Exception:
        return False


def start_bundled_backend():
    """Start this app bundle's Wiredrive engine on a private dynamic port."""
    global http_server, server_thread

    wiredrive.init_db()
    wiredrive.start_observers()
    wiredrive.ensure_worker()
    wiredrive.ensure_sync_workers()

    # Port 0 asks macOS for an unused ephemeral localhost port. This deliberately
    # avoids port 8765 so an older browser/watch-folder build can never be reused.
    http_server = make_server(HOST, 0, wiredrive.app, threaded=True)
    port = int(http_server.server_port)
    url = f"http://{HOST}:{port}/"

    server_thread = threading.Thread(
        target=http_server.serve_forever,
        daemon=True,
        name="WiredriveSyncBundledHTTP",
    )
    server_thread.start()

    deadline = time.time() + 20
    while time.time() < deadline:
        if backend_responding(url):
            return url
        time.sleep(0.15)

    raise RuntimeError("The bundled Wiredrive Sync engine did not start within 20 seconds.")


def cleanup():
    global http_server
    try:
        wiredrive.stop_observers()
    except Exception:
        pass
    try:
        if http_server is not None:
            http_server.shutdown()
            http_server.server_close()
            http_server = None
    except Exception:
        pass


def show_startup_error(message):
    try:
        webview.create_window(
            "Wiredrive Sync — Startup Error",
            html=f"""
            <html>
              <body style="margin:0;background:#08131d;color:#e8eef4;
                           font-family:-apple-system,BlinkMacSystemFont,sans-serif">
                <div style="padding:38px">
                  <h2 style="margin-top:0">Wiredrive Sync could not start</h2>
                  <p style="line-height:1.55;color:#9eafbd">{message}</p>
                </div>
              </body>
            </html>
            """,
            width=660,
            height=320,
            resizable=False,
            background_color="#08131d",
        )
        webview.start(gui="cocoa", debug=False)
    except Exception:
        print(message, file=sys.stderr)




_about_handler = None

def _build_about_handler():
    """Create and retain the native Wiredrive Sync About-panel target."""
    global _about_handler
    try:
        from Cocoa import NSObject, NSApplication, NSImage
        import objc
        class WiredriveSyncAboutHandler(NSObject):
            @objc.IBAction
            def showAbout_(self, sender):
                try:
                    icon_path = str((HERE.parent / "AppIcon.icns").resolve())
                    icon = NSImage.alloc().initWithContentsOfFile_(icon_path)
                    options = {
                        "ApplicationName": "Wiredrive Sync",
                        "ApplicationVersion": "1.7",
                        "Version": "Build 170",
                        "Copyright": "© 2026 Three Crow Studios",
                    }
                    if icon is not None:
                        options["ApplicationIcon"] = icon
                    NSApplication.sharedApplication().orderFrontStandardAboutPanelWithOptions_(options)
                except Exception as exc:
                    print(f"Could not display Wiredrive Sync About panel: {exc}", file=sys.stderr)
        _about_handler = WiredriveSyncAboutHandler.alloc().init()
        return _about_handler
    except Exception as exc:
        print(f"Could not create Wiredrive Sync About handler: {exc}", file=sys.stderr)
        return None

def _apply_cocoa_identity_once():
    """Apply Wiredrive Sync identity to the actual Cocoa application menu."""
    try:
        from Cocoa import NSApplication
        from Foundation import NSProcessInfo

        NSProcessInfo.processInfo().setProcessName_("Wiredrive Sync")
        app = NSApplication.sharedApplication()
        menu = app.mainMenu()
        if menu is None or menu.numberOfItems() == 0:
            return False

        app_item = menu.itemAtIndex_(0)
        app_item.setTitle_("Wiredrive Sync")
        submenu = app_item.submenu()
        if submenu is None:
            return True

        about_handler = _build_about_handler()
        for i in range(submenu.numberOfItems()):
            item = submenu.itemAtIndex_(i)
            title = str(item.title() or "")

            if title in ("About Python", "About Wiredrive Sync"):
                item.setTitle_("About Wiredrive Sync")
                if about_handler is not None:
                    item.setTarget_(about_handler)
                    item.setAction_("showAbout:")
            elif title in ("Hide Python", "Hide Wiredrive Sync"):
                item.setTitle_("Hide Wiredrive Sync")
            elif title in ("Quit Python", "Quit Wiredrive Sync"):
                item.setTitle_("Quit Wiredrive Sync")
        return True
    except Exception as exc:
        try:
            print(f"Cocoa identity apply failed: {exc}", file=sys.stderr)
        except Exception:
            pass
        return False


def _configure_cocoa_identity():
    """Keep Wiredrive Sync identity after pywebview finishes rebuilding menus."""
    try:
        from PyObjCTools import AppHelper

        # Always mutate AppKit on the main thread. pywebview may rebuild the
        # application menu shortly after its startup callback, so reassert the
        # identity over the first few seconds instead of doing a one-shot rename.
        def apply_on_main():
            AppHelper.callAfter(_apply_cocoa_identity_once)

        apply_on_main()
        for delay in (0.15, 0.35, 0.75, 1.5, 3.0, 5.0):
            timer = threading.Timer(delay, apply_on_main)
            timer.daemon = True
            timer.start()
    except Exception as exc:
        print(f"Could not schedule Cocoa identity fix: {exc}", file=sys.stderr)


def main():
    atexit.register(cleanup)

    try:
        url = start_bundled_backend()
    except Exception as exc:
        show_startup_error(str(exc))
        return 1

    webview.create_window(
        "Wiredrive Sync",
        url,
        width=1536,
        height=960,
        min_size=(1100, 720),
        resizable=True,
        background_color="#07121d",
        text_select=True,
    )

    webview.start(_configure_cocoa_identity, gui="cocoa", debug=False)
    cleanup()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
