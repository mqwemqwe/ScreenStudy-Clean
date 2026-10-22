"""Installer checks native API presence before producing the bundle."""
import sys

if sys.platform != "darwin":
    raise SystemExit("Этот тест запускается только на macOS.")

import AppKit
import ApplicationServices as AX
import Quartz
import ScreenCaptureKit as SC
import Vision

checks = [
    hasattr(SC.SCScreenshotManager, 'captureImageWithFilter_configuration_completionHandler_'),
    hasattr(SC.SCContentFilter, 'initWithDisplay_excludingApplications_exceptingWindows_'),
    hasattr(SC.SCShareableContent, 'getShareableContentWithCompletionHandler_'),
    hasattr(Vision.VNRecognizeTextRequest, 'supportedRecognitionLanguagesAndReturnError_'),
    hasattr(AX, 'AXIsProcessTrustedWithOptions'),
    hasattr(Quartz, 'CGPreflightScreenCaptureAccess'),
    hasattr(AppKit, 'NSBitmapImageFileTypePNG'),
]
if not all(checks):
    raise SystemExit('Не все системные API доступны: ' + repr(checks))
print('ScreenCaptureKit, Vision и Accessibility доступны.')
