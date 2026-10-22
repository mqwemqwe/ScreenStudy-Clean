"""macOS boundary. All captures exclude this application's windows."""
from __future__ import annotations

import os
import sys
import threading
import time


def require_mac():
    if sys.platform != "darwin":
        raise RuntimeError("Захват экрана доступен на macOS 14 и новее.")


def displays():
    require_mac()
    import AppKit
    import Quartz
    return [{"id": int(s.deviceDescription()["NSScreenNumber"]),
             "name": str(s.localizedName()),
             "main": int(s.deviceDescription()["NSScreenNumber"]) == Quartz.CGMainDisplayID()}
            for s in AppKit.NSScreen.screens()]


def request_screen_permission():
    require_mac()
    import Quartz
    return bool(Quartz.CGPreflightScreenCaptureAccess() or Quartz.CGRequestScreenCaptureAccess())


def capture(display_id, crop, previous=None):
    """Called on a background thread; pump its CF runloop for SCK completions."""
    require_mac()
    import objc
    import AppKit
    import Quartz
    import Vision
    import ScreenCaptureKit as SC
    from Foundation import NSRunLoop, NSDate

    with objc.autorelease_pool():
        if not Quartz.CGPreflightScreenCaptureAccess():
            raise RuntimeError("Разрешите ScreenStudy запись экрана в настройках macOS, затем перезапустите приложение.")
        done = threading.Event()
        holder = {}

        def got_image(image, error):
            if error:
                holder["error"] = str(error.localizedDescription())
            elif image is None:
                holder["error"] = "macOS вернула пустой снимок."
            else:
                holder["image"] = image
            done.set()

        def got_content(content, error):
            try:
                if error:
                    raise RuntimeError(str(error.localizedDescription()))
                wanted = display_id or Quartz.CGMainDisplayID()
                display = next((d for d in content.displays() if d.displayID() == wanted), None)
                if display is None:
                    raise RuntimeError("Выбранный экран отключён. Выберите экран в настройках.")
                own = [app for app in content.applications() if app.processID() == os.getpid()]
                filt = SC.SCContentFilter.alloc().initWithDisplay_excludingApplications_exceptingWindows_(
                    display, own, [])
                conf = SC.SCStreamConfiguration.alloc().init()
                conf.setWidth_(int(display.width()))
                conf.setHeight_(int(display.height()))
                conf.setShowsCursor_(False)
                if hasattr(conf, "setCaptureResolution_") and hasattr(SC, "SCCaptureResolutionBest"):
                    conf.setCaptureResolution_(SC.SCCaptureResolutionBest)
                # Keep Objective-C filter/config alive until completion.
                holder["objects"] = (content, filt, conf)
                bounds = display.frame()
                holder["bounds"] = (float(bounds.origin.x), float(bounds.origin.y),
                                    float(bounds.size.width), float(bounds.size.height))
                SC.SCScreenshotManager.captureImageWithFilter_configuration_completionHandler_(
                    filt, conf, got_image)
            except Exception as exc:
                holder["error"] = str(exc)
                done.set()

        SC.SCShareableContent.getShareableContentWithCompletionHandler_(got_content)
        deadline = time.monotonic() + 15
        while not done.is_set() and time.monotonic() < deadline:
            NSRunLoop.currentRunLoop().runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(0.025))
            done.wait(0.005)
        if not done.is_set():
            raise RuntimeError("macOS не ответила на запрос снимка. Проверьте разрешение записи экрана.")
        if "error" in holder:
            raise RuntimeError(holder["error"])
        original = holder["image"]
        iw, ih = Quartz.CGImageGetWidth(original), Quartz.CGImageGetHeight(original)
        rect = Quartz.CGRectMake(int(crop[0]*iw), int(crop[1]*ih),
                                 max(1, int(crop[2]*iw)), max(1, int(crop[3]*ih)))
        image = Quartz.CGImageCreateWithImageInRect(original, rect)
        if image is None:
            raise RuntimeError("Область снимка недоступна. Выберите область заново.")
        rep = AppKit.NSBitmapImageRep.alloc().initWithCGImage_(image)
        png = bytes(rep.representationUsingType_properties_(AppKit.NSBitmapImageFileTypePNG, {}))
        x, y, w, h = holder["bounds"]
        bounds = [x+crop[0]*w, y+crop[1]*h, crop[2]*w, crop[3]*h]
        # Reuse OCR only for byte-identical cropped pixels. Nothing is saved to disk.
        if previous is not None and previous.get("png") == png:
            return {"png": png, "lines": previous["lines"], "bounds": bounds, "ocr_cached": True}
        request = Vision.VNRecognizeTextRequest.alloc().init()
        request.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
        request.setUsesLanguageCorrection_(True)
        supported, _ = request.supportedRecognitionLanguagesAndReturnError_(None)
        languages = [lang for lang in ("ru-RU", "en-US") if lang in (supported or [])]
        if languages:
            request.setRecognitionLanguages_(languages)
        handler = Vision.VNImageRequestHandler.alloc().initWithCGImage_options_(image, {})
        ok, error = handler.performRequests_error_([request], None)
        if not ok:
            raise RuntimeError("Не удалось распознать текст: " + str(error))
        found = []
        for observation in request.results() or []:
            candidates = observation.topCandidates_(1)
            if not candidates:
                continue
            b = observation.boundingBox()
            found.append({"text": str(candidates[0].string()),
                          "box": [float(b.origin.x), float(1-b.origin.y-b.size.height),
                                  float(b.size.width), float(b.size.height)]})
        found.sort(key=lambda v: (round(v["box"][1], 2), v["box"][0]))
        lines = [dict(id=i, **v) for i, v in enumerate(found[:400])]
        return {"png": png, "lines": lines, "bounds": bounds, "ocr_cached": False}


def accessibility(prompt=False):
    require_mac()
    import ApplicationServices as AX
    return bool(AX.AXIsProcessTrustedWithOptions({AX.kAXTrustedCheckOptionPrompt: bool(prompt)}))


def paste():
    require_mac()
    import AppKit
    import Quartz
    if not accessibility():
        raise RuntimeError("Разрешите ScreenStudy «Универсальный доступ». Ответ уже скопирован — можно нажать ⌘V вручную.")
    app = AppKit.NSWorkspace.sharedWorkspace().frontmostApplication()
    if app is None or app.processIdentifier() == os.getpid():
        raise RuntimeError("Сначала нажмите в поле ответа другого приложения. Текст уже скопирован.")
    # No Return and no select-all. Paste only at the user's current insertion point.
    source = Quartz.CGEventSourceCreate(Quartz.kCGEventSourceStateHIDSystemState)
    for down in (True, False):
        event = Quartz.CGEventCreateKeyboardEvent(source, 9, down)  # ANSI V
        Quartz.CGEventSetFlags(event, Quartz.kCGEventFlagMaskCommand)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)


def configure_overlay(widget, clickable):
    if sys.platform != "darwin":
        return
    import objc
    import AppKit
    view = objc.objc_object(c_void_p=int(widget.winId()))
    win = view.window()
    win.setLevel_(AppKit.NSStatusWindowLevel)
    win.setHidesOnDeactivate_(False)
    win.setCollectionBehavior_(AppKit.NSWindowCollectionBehaviorCanJoinAllSpaces |
                               AppKit.NSWindowCollectionBehaviorFullScreenAuxiliary)
    win.setIgnoresMouseEvents_(not clickable)


def read_key(account):
    if sys.platform != "darwin":
        return ""
    from keyring.backends.macOS import Keyring
    return Keyring().get_password("ScreenStudy", account) or ""


def save_key(account, value):
    require_mac()
    from keyring.backends.macOS import Keyring
    from keyring.errors import PasswordDeleteError
    ring = Keyring()
    if value:
        ring.set_password("ScreenStudy", account, value)
    else:
        try:
            ring.delete_password("ScreenStudy", account)
        except PasswordDeleteError:
            pass
