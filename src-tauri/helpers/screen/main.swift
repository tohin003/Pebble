// pebble-screen-helper: Pebble's screen helper (Phase 43, 1.9.0, M3; decisions 5 and 6). Protocol: README.md here.
//
// One JSON request per line on stdin, one JSON answer per line on stdout (protocol pebble-screen/1). Every answer is
// bounded. An image is written only inside the mode-700 folder of Pebble's that the request names, and nothing is
// kept between requests except the input lease. It never prompts for a permission: hello only preflights, and capture
// refuses before ScreenCaptureKit is touched when Screen Recording is not allowed. The five coordinate spaces belong
// to the host; this reports raw facts only: image pixels, the window's frame in global points, the backing scale.
//
// Input is foreground CGEvent input under a lease the host holds (one session across the Mac). It needs a reliable way
// to notice the person's own input: a listen-only event tap (Input Monitoring). Every event this helper posts carries
// EVENT_MARK in its source user data, so the tap tells its own events from the person's without reading a keystroke.
// Any input of the person's, a secure input field, or a lost lease stops it; held keys and buttons are released on
// every exit path that runs code (an answer, an error, end of input, SIGTERM, SIGINT, SIGHUP).
//
// Phase 45 (layer 4): a click or scroll may name the window that must own its point (the topmost visible window
// there, read from the window server just before the first event), and a key or typing the process that must hold the
// keyboard (Accessibility's focused application); either refuses before any event otherwise. An input may wait up to
// wait_seconds for the person to be quiet (they may have just approved it with a click). release_all from a fresh
// helper also lifts keys and buttons the session still holds down that the hardware does not (a killed helper's).
// F258 (2026-10-27): after a key act the modifier state is put back as it was before it (restoreModifiers).
// F262 (2026-10-27): a drag presses at one point and releases at another, both in the named window.

import ApplicationServices
import Carbon.HIToolbox
import CoreGraphics
import CryptoKit
import Foundation
import ImageIO
import ScreenCaptureKit
import UniformTypeIdentifiers
import Vision

let PROTOCOL = "pebble-screen/1"
let MAX_REQUEST = 65_536
let MAX_ANSWER = 1_048_576
let MAX_LINES = 400
let MAX_LINE_TEXT = 500
let MAX_TYPED = 500
let MAX_RECTS = 64
let MAX_PIDS = 16
let MAX_LEASE_SECONDS = 120.0
let CAPTURE_SECONDS = 8.0
let EVENT_MARK: Int64 = 0x5045_4242_4C45  // "PEBBLE": the source user data of every event this helper posts

struct Failure: Error {
    let code: String
    let detail: String
}

func fail(_ code: String, _ detail: String) -> Failure { Failure(code: code, detail: detail) }

let invalid = { (what: String) in fail("invalid", what) }

// ---- answers -------------------------------------------------------------------------------------------------------

let JSON_OPTIONS: JSONSerialization.WritingOptions = [.sortedKeys, .withoutEscapingSlashes]

func answer(_ object: [String: Any]) {
    var data = (try? JSONSerialization.data(withJSONObject: object, options: JSON_OPTIONS)) ?? Data()
    if data.isEmpty || data.count > MAX_ANSWER {
        let refusal: [String: Any] = ["id": object["id"] ?? NSNull(), "ok": false, "error": "protocol",
                                      "detail": "The answer could not be encoded within its size limit."]
        data = (try? JSONSerialization.data(withJSONObject: refusal, options: JSON_OPTIONS)) ?? Data()
    }
    data.append(0x0A)
    FileHandle.standardOutput.write(data)
}

func finite(_ value: Double) -> Double { value.isFinite ? value : 0 }

// ---- hello ---------------------------------------------------------------------------------------------------------

typealias CreateImage = @convention(c) (CGRect, UInt32, UInt32, UInt32) -> Unmanaged<CGImage>?

// The pre-ScreenCaptureKit call, looked up at run time (the current SDK marks it unavailable); used below macOS 14.
let cgCreateImage: CreateImage? = {
    guard let symbol = dlsym(UnsafeMutableRawPointer(bitPattern: -2), "CGWindowListCreateImage") else { return nil }
    return unsafeBitCast(symbol, to: CreateImage.self)
}()

func captureAPI() -> String {
    if #available(macOS 14.0, *) { return "screencapturekit" }
    return cgCreateImage != nil ? "cgwindowlist" : "none"
}

func hello() -> [String: Any] {
    let version = ProcessInfo.processInfo.operatingSystemVersion
    let listen = CGPreflightListenEventAccess()
    return ["protocol": PROTOCOL,
            "macos": "\(version.majorVersion).\(version.minorVersion).\(version.patchVersion)",
            "screen_recording": CGPreflightScreenCaptureAccess(), "accessibility": AXIsProcessTrusted(),
            "post_events": CGPreflightPostEventAccess(), "listen_events": listen,
            "input_detection": listen ? "event_tap" : "none", "capture_api": captureAPI(),
            "secure_input": IsSecureEventInputEnabled()]
}

// ---- arguments -----------------------------------------------------------------------------------------------------

func windowNumber(_ args: [String: Any]) throws -> CGWindowID {
    guard let value = args["window_number"] as? Int, value > 0, value <= Int(UInt32.max) else {
        throw invalid("window_number")
    }
    return CGWindowID(value)
}

/// The host's private folder: an absolute, normalised path to a real directory (no link) owned by this user and closed
/// to everyone else (mode 700).
func privateFolder(_ args: [String: Any]) throws -> String {
    guard let path = args["folder"] as? String, path.hasPrefix("/"), path.utf8.count <= 1024, !path.contains("\0"),
          URL(fileURLWithPath: path).standardized.path == path else { throw invalid("folder") }
    var info = stat()
    guard lstat(path, &info) == 0, info.st_mode & S_IFMT == S_IFDIR, info.st_uid == getuid(),
          info.st_mode & 0o077 == 0 else {
        throw fail("refused", "The folder must be Pebble's own, closed to others (mode 700).")
    }
    return path
}

func windowInfo(_ number: CGWindowID) -> [String: Any]? {
    let raw = CGWindowListCopyWindowInfo([.optionIncludingWindow], number)
    guard let list = raw as? [[String: Any]] else { return nil }
    return list.first { ($0[kCGWindowNumber as String] as? Int).map { CGWindowID($0) } == number }
}

func frameOf(_ info: [String: Any]) -> CGRect? {
    guard let bounds = info[kCGWindowBounds as String] as? NSDictionary,
          let frame = CGRect(dictionaryRepresentation: bounds as CFDictionary), frame.width > 0, frame.height > 0 else {
        return nil
    }
    return frame
}

// ---- capture -------------------------------------------------------------------------------------------------------

final class Box<T>: @unchecked Sendable {
    var value: T?
}

func shoot(_ number: CGWindowID) throws -> CGImage {
    if #available(macOS 14.0, *) {
        let done = DispatchSemaphore(value: 0)
        let box = Box<Result<CGImage, Error>>()
        Task.detached {
            do {
                let content = try await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: false)
                guard let window = content.windows.first(where: { $0.windowID == number }) else {
                    throw fail("not_found", "The window is gone.")
                }
                // This one window's own content: every other window (Pebble's included) is left out by construction.
                let filter = SCContentFilter(desktopIndependentWindow: window)
                let config = SCStreamConfiguration()
                let scale = CGFloat(filter.pointPixelScale)
                config.width = max(1, Int((filter.contentRect.width * scale).rounded()))
                config.height = max(1, Int((filter.contentRect.height * scale).rounded()))
                config.showsCursor = false
                config.ignoreShadowsSingleWindow = true
                config.captureResolution = .best
                let image = try await SCScreenshotManager.captureImage(contentFilter: filter, configuration: config)
                box.value = .success(image)
            } catch {
                box.value = .failure(error)
            }
            done.signal()
        }
        if done.wait(timeout: .now() + CAPTURE_SECONDS) == .timedOut {
            throw fail("timeout", "The capture did not finish in time.")
        }
        switch box.value {
        case .success(let image)?: return image
        case .failure(let error as Failure)?: throw error
        case .failure(let error)?: throw fail("failed", String(describing: type(of: error)))
        case nil: throw fail("failed", "No image.")
        }
    }
    // Below macOS 14: kCGWindowListOptionIncludingWindow, BoundsIgnoreFraming | BestResolution.
    guard let create = cgCreateImage, let image = create(.null, 1 << 3, number, (1 << 0) | (1 << 3)) else {
        throw fail("unsupported", "No capture call on this macOS.")
    }
    return image.takeRetainedValue()
}

/// The PNG of image, written as a new file (O_EXCL, no link followed, mode 600) in folder.
func writePNG(_ image: CGImage, _ folder: String) throws -> (String, Data) {
    let data = NSMutableData()
    guard let destination = CGImageDestinationCreateWithData(data as CFMutableData, UTType.png.identifier as CFString,
                                                             1, nil) else { throw fail("failed", "No PNG writer.") }
    CGImageDestinationAddImage(destination, image, nil)
    guard CGImageDestinationFinalize(destination) else { throw fail("failed", "The PNG could not be made.") }
    let name = "capture-\(UInt64(Date().timeIntervalSince1970 * 1000))-\(UInt32.random(in: 0...UInt32.max)).png"
    let path = folder + "/" + name
    let fd = open(path, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW, 0o600)
    guard fd >= 0 else { throw fail("failed", "The image file could not be made.") }
    defer { close(fd) }
    let bytes = data as Data
    let written = bytes.withUnsafeBytes { write(fd, $0.baseAddress, bytes.count) }
    guard written == bytes.count else {
        unlink(path)
        throw fail("failed", "The image file could not be written.")
    }
    return (path, bytes)
}

func capture(_ args: [String: Any]) throws -> [String: Any] {
    let number = try windowNumber(args)
    let folder = try privateFolder(args)
    let own = args["own_pids"] as? [Int] ?? []
    guard own.count <= MAX_PIDS else { throw invalid("own_pids") }
    guard CGPreflightScreenCaptureAccess() else {
        throw fail("not_permitted", "Pebble is not allowed to record the screen (System Settings > Privacy & "
                   + "Security > Screen & System Audio Recording).")
    }
    guard let info = windowInfo(number), let frame = frameOf(info) else {
        throw fail("not_found", "The window is gone.")
    }
    let owner = info[kCGWindowOwnerPID as String] as? Int ?? -1
    if owner == Int(getpid()) || own.contains(owner) {
        throw fail("refused", "Pebble never captures its own windows.")
    }
    let image = try shoot(number)
    let (path, bytes) = try writePNG(image, folder)
    let digest = SHA256.hash(data: bytes).map { String(format: "%02x", $0) }.joined()
    return ["path": path, "pixels": [image.width, image.height], "bytes": bytes.count, "sha256": digest,
            "frame": [finite(frame.minX), finite(frame.minY), finite(frame.width), finite(frame.height)],
            "scale": finite(Double(image.width) / Double(frame.width)), "owner_pid": owner,
            "on_screen": info[kCGWindowIsOnscreen as String] as? Bool ?? false,
            "layer": info[kCGWindowLayer as String] as? Int ?? 0]
}

// ---- text recognition ----------------------------------------------------------------------------------------------

func capturedImage(_ args: [String: Any]) throws -> CGImage {
    let folder = try privateFolder(args)
    guard let path = args["path"] as? String, (path as NSString).deletingLastPathComponent == folder,
          (path as NSString).lastPathComponent.hasPrefix("capture-"), path.hasSuffix(".png") else {
        throw invalid("path")
    }
    var info = stat()
    guard lstat(path, &info) == 0, info.st_mode & S_IFMT == S_IFREG, info.st_uid == getuid() else {
        throw fail("not_found", "The image is gone.")
    }
    guard let source = CGImageSourceCreateWithURL(URL(fileURLWithPath: path) as CFURL, nil),
          let image = CGImageSourceCreateImageAtIndex(source, 0, nil) else {
        throw fail("failed", "The image cannot be read.")
    }
    return image
}

/// Rectangles the host blanks before any text is read (secure fields), in image pixels from the top left.
func blankRects(_ args: [String: Any], _ image: CGImage) throws -> [CGRect] {
    guard let raw = args["blank"] else { return [] }
    guard let items = raw as? [[Double]], items.count <= MAX_RECTS else { throw invalid("blank") }
    return try items.map { item in
        guard item.count == 4, item.allSatisfy({ $0.isFinite }), item[2] > 0, item[3] > 0 else {
            throw invalid("blank")
        }
        return CGRect(x: item[0], y: item[1], width: item[2], height: item[3])
    }
}

func blanked(_ image: CGImage, _ rects: [CGRect]) throws -> CGImage {
    let width = image.width, height = image.height
    guard let space = CGColorSpace(name: CGColorSpace.sRGB),
          let context = CGContext(data: nil, width: width, height: height, bitsPerComponent: 8, bytesPerRow: 0,
                                  space: space, bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue) else {
        throw fail("failed", "The image cannot be prepared.")
    }
    context.draw(image, in: CGRect(x: 0, y: 0, width: width, height: height))
    context.setFillColor(CGColor(red: 0, green: 0, blue: 0, alpha: 1))
    for rect in rects {  // the host's rectangles count from the top; CoreGraphics from the bottom
        context.fill(CGRect(x: rect.minX, y: CGFloat(height) - rect.maxY, width: rect.width, height: rect.height))
    }
    guard let made = context.makeImage() else { throw fail("failed", "The image cannot be prepared.") }
    return made
}

func ocr(_ args: [String: Any]) throws -> [String: Any] {
    var image = try capturedImage(args)
    let rects = try blankRects(args, image)
    if !rects.isEmpty { image = try blanked(image, rects) }
    let request = VNRecognizeTextRequest()
    request.recognitionLevel = (args["level"] as? String) == "fast" ? .fast : .accurate
    request.usesLanguageCorrection = true
    do {
        try VNImageRequestHandler(cgImage: image, options: [:]).perform([request])
    } catch {
        throw fail("failed", "Text recognition failed.")
    }
    let width = Double(image.width), height = Double(image.height)
    let found = request.results ?? []
    var lines: [[String: Any]] = []
    for observation in found.prefix(MAX_LINES) {
        guard let top = observation.topCandidates(1).first else { continue }
        let box = observation.boundingBox  // normalised, from the bottom left
        lines.append(["text": String(top.string.prefix(MAX_LINE_TEXT)), "confidence": finite(Double(top.confidence)),
                      "box": [finite(box.minX * width), finite((1 - box.maxY) * height), finite(box.width * width),
                              finite(box.height * height)]])
    }
    return ["lines": lines, "pixels": [image.width, image.height], "blanked": rects.count,
            "truncated": found.count > MAX_LINES]
}

// ---- input ---------------------------------------------------------------------------------------------------------

final class Watch: @unchecked Sendable {
    let lock = NSLock()
    var lastPerson: UInt64 = 0     // uptime ns of the person's newest event the tap saw (0: none yet)
    var running = false
    var tap: CFMachPort?

    func mark() {
        lock.lock()
        lastPerson = DispatchTime.now().uptimeNanoseconds
        lock.unlock()
    }

    func last() -> UInt64 {
        lock.lock()
        defer { lock.unlock() }
        return lastPerson
    }
}

let watch = Watch()

/// The listen-only event tap on its own thread: it only notes when an event without EVENT_MARK arrived (the person's),
/// never which key or where.
func startWatch() -> Bool {
    if watch.running { return true }
    guard CGPreflightListenEventAccess() else { return false }
    var mask: CGEventMask = 0
    for type in [CGEventType.keyDown, .keyUp, .flagsChanged, .leftMouseDown, .leftMouseUp, .rightMouseDown,
                 .rightMouseUp, .otherMouseDown, .otherMouseUp, .mouseMoved, .leftMouseDragged, .rightMouseDragged,
                 .scrollWheel] {
        mask |= CGEventMask(1) << CGEventMask(type.rawValue)
    }
    guard let tap = CGEvent.tapCreate(tap: .cgSessionEventTap, place: .headInsertEventTap, options: .listenOnly,
                                      eventsOfInterest: mask, callback: { _, type, event, _ in
        if type == .tapDisabledByTimeout || type == .tapDisabledByUserInput {
            if let tap = watch.tap { CGEvent.tapEnable(tap: tap, enable: true) }
        } else if event.getIntegerValueField(.eventSourceUserData) != EVENT_MARK {
            watch.mark()
        }
        return Unmanaged.passUnretained(event)
    }, userInfo: nil) else { return false }
    watch.tap = tap
    let thread = Thread {
        let source = CFMachPortCreateRunLoopSource(kCFAllocatorDefault, tap, 0)
        CFRunLoopAddSource(CFRunLoopGetCurrent(), source, .commonModes)
        CGEvent.tapEnable(tap: tap, enable: true)
        CFRunLoopRun()
    }
    thread.name = "pebble-screen-watch"
    thread.start()
    watch.running = true
    return true
}

var lease: (token: [UInt8], until: UInt64)?
var heldKeys = Set<CGKeyCode>()
var modifiersInAct: (CGEventFlags, CGEventFlags)?  // a key act in progress: the state before it, the modifiers it uses
var heldButtons = Set<UInt32>()  // CGMouseButton raw values
let source = CGEventSource(stateID: .privateState)

func post(_ event: CGEvent?) throws {
    guard let event else { throw fail("failed", "The event could not be made.") }
    event.setIntegerValueField(.eventSourceUserData, value: EVENT_MARK)
    event.post(tap: .cghidEventTap)
}

/// The modifier flags a key event leaves behind. Phase 45 (F258, measured 2026-10-27 on a real TextEdit document,
/// .runtime/phase45-dev/native/modifiers-1.9.17.jsonl): the window server keeps the flags of the last keyboard event
/// posted at the HID tap as the login session's modifier state, in both the session's and the HID system's view, so
/// after ⌘↑ the ⌘ key (55) and Fn read as held, after ⇧End Shift and Fn, after a plain arrow or End Fn (arrows and End
/// carry Fn): the person's next keys were dropped or acted as shortcuts until some later event cleared it.
let HELD_FLAGS: CGEventFlags = [.maskCommand, .maskShift, .maskAlternate, .maskControl, .maskSecondaryFn,
                                .maskAlphaShift]
let RELEASE_ORDER: [(CGEventFlags, Int)] = [(.maskSecondaryFn, kVK_Function), (.maskControl, kVK_Control),
                                            (.maskAlternate, kVK_Option), (.maskShift, kVK_Shift),
                                            (.maskCommand, kVK_Command)]

/// Keys that carry Fn in their events, as a Mac keyboard's do (the arrows, Home, End, Page Up/Down, forward delete).
let FN_CARRIERS: Set<Int> = [kVK_LeftArrow, kVK_RightArrow, kVK_UpArrow, kVK_DownArrow, kVK_Home, kVK_End,
                             kVK_PageUp, kVK_PageDown, kVK_ForwardDelete]

/// Puts the session's modifier state back to `before` (read before this helper's key act) after a key act that used
/// `used` (its chord's modifiers, and Fn for a key that carries it): one flagsChanged event per such modifier, in
/// release order, each naming that modifier's key, the last one carrying exactly `before`. The state is not read back
/// here: the window server applies posted events in order but later, so a read right after the key-up still shows the
/// state before it (measured: a first version that read it released nothing about half the time). Returns how many
/// events were posted.
func restoreModifiers(_ before: CGEventFlags, used: CGEventFlags) -> Int {
    let target = before.intersection(HELD_FLAGS)
    var now = used.union(target).intersection(HELD_FLAGS)
    var posted = 0
    for (flag, key) in RELEASE_ORDER where now.contains(flag) && !target.contains(flag) {
        now.remove(flag)
        let event = CGEvent(keyboardEventSource: source, virtualKey: CGKeyCode(key), keyDown: false)
        event?.type = .flagsChanged
        event?.flags = now
        try? post(event)
        posted += 1
    }
    return posted
}

/// Every key and button this helper holds down goes up again, the modifiers first.
func releaseAll() {
    let keys = !heldKeys.isEmpty
    for key in heldKeys.sorted() {
        try? post(CGEvent(keyboardEventSource: source, virtualKey: key, keyDown: false))
    }
    heldKeys.removeAll()
    if keys, let (before, used) = modifiersInAct { _ = restoreModifiers(before, used: used) }
    modifiersInAct = nil
    let here = CGEvent(source: nil)?.location ?? .zero
    for raw in heldButtons.sorted() {
        let button = CGMouseButton(rawValue: raw) ?? .left
        let type: CGEventType = button == .left ? .leftMouseUp : button == .right ? .rightMouseUp : .otherMouseUp
        try? post(CGEvent(mouseEventSource: source, mouseType: type, mouseCursorPosition: here, mouseButton: button))
    }
    heldButtons.removeAll()
}

/// Phase 45: after a helper was killed mid-input (the host sends release_all from a fresh one), the keys of KEYS and
/// the mouse buttons the login session still holds down while the hardware does not were posted by that helper; each
/// goes up once. A key or button the person physically holds is left alone. Returns how many were lifted.
func releaseLeftovers() -> Int {
    var lifted = 0
    for code in Set(KEYS.values).sorted() {
        let key = CGKeyCode(code)
        if CGEventSource.keyState(.combinedSessionState, key: key) && !CGEventSource.keyState(.hidSystemState, key: key) {
            try? post(CGEvent(keyboardEventSource: source, virtualKey: key, keyDown: false))
            lifted += 1
        }
    }
    let here = CGEvent(source: nil)?.location ?? .zero
    for (button, type) in [(CGMouseButton.left, CGEventType.leftMouseUp), (.right, .rightMouseUp),
                           (.center, .otherMouseUp)] {
        if CGEventSource.buttonState(.combinedSessionState, button: button)
            && !CGEventSource.buttonState(.hidSystemState, button: button) {
            try? post(CGEvent(mouseEventSource: source, mouseType: type, mouseCursorPosition: here, mouseButton: button))
            lifted += 1
        }
    }
    return lifted
}

/// Phase 45: the window-server number of the topmost visible window at a global point (top-left origin): on-screen
/// windows front to back, desktop elements left out, a fully transparent window skipped; nil when none is there.
/// Measured 2026-10-26 04:46 and 06:16 (`.runtime/phase45-dev/l4/ec4-diagnosis.md`): the Dock keeps a screen-sized
/// window at its own level over every app, even with desktop elements left out, and it takes clicks only on the Dock
/// itself, so every point read as covered. The Dock's and the window server's overlays are skipped; a point on the Dock
/// itself is refused by the host's Accessibility check before the act (another process at the point).
let overlayOwners: Set<String> = ["Dock", "Window Server", "WindowManager"]
func topWindow(at point: CGPoint) -> Int? {
    let raw = CGWindowListCopyWindowInfo([.optionOnScreenOnly, .excludeDesktopElements], kCGNullWindowID)
    guard let list = raw as? [[String: Any]] else { return nil }
    for info in list {
        guard let frame = frameOf(info), frame.contains(point) else { continue }
        if let alpha = info[kCGWindowAlpha as String] as? Double, alpha <= 0 { continue }
        if let owner = info[kCGWindowOwnerName as String] as? String, overlayOwners.contains(owner) { continue }
        return info[kCGWindowNumber as String] as? Int
    }
    return nil
}

/// Phase 45: the process holding the keyboard (Accessibility's system-wide focused application), or nil when it
/// cannot be read (Accessibility not allowed, or no answer).
/// Phase 45 (measured 2026-10-26 06:30, `.runtime/phase45-dev/l4/ec4-diagnosis.md`): the system-wide element's focused
/// application answers "cannot complete" on this macOS whatever app is in front, so every typing act was refused. When
/// it cannot answer, the granted app holds the keyboard if it is the active app (its own frontmost flag): a click
/// elsewhere since then makes another app the active one, and the act is refused.
func holdsKeyboard(_ pid: Int) -> Bool {
    if let found = focusedProcess() { return found == pid }
    guard AXIsProcessTrusted() else { return false }
    let app = AXUIElementCreateApplication(pid_t(pid))
    AXUIElementSetMessagingTimeout(app, 1.0)
    var value: CFTypeRef?
    guard AXUIElementCopyAttributeValue(app, kAXFrontmostAttribute as CFString, &value) == .success,
          let frontmost = value as? Bool else { return false }
    return frontmost
}

func focusedProcess() -> Int? {
    guard AXIsProcessTrusted() else { return nil }
    let system = AXUIElementCreateSystemWide()
    AXUIElementSetMessagingTimeout(system, 1.0)
    var value: CFTypeRef?
    guard AXUIElementCopyAttributeValue(system, kAXFocusedApplicationAttribute as CFString, &value) == .success,
          let found = value, CFGetTypeID(found) == AXUIElementGetTypeID() else { return nil }
    var pid: pid_t = 0
    guard AXUIElementGetPid(found as! AXUIElement, &pid) == .success, pid > 0 else { return nil }
    return Int(pid)
}

/// Phase 45: before any event, the point's owner window (click, scroll) or the keyboard's process (key, type), when the
/// host names one.
func targetHolds(_ kind: String, _ args: [String: Any]) throws {
    if kind == "click" || kind == "scroll" || kind == "drag", let expected = args["window_number"] {
        guard let number = expected as? Int, number > 0 else { throw invalid("window_number") }
        guard topWindow(at: try point(args)) == number else {
            throw fail("refused", "Another window covers that point now, so Pebble does not click there.")
        }
        if kind == "drag", topWindow(at: try point(args, "to")) != number {
            throw fail("refused", "Another window covers the end of the drag now, so Pebble does not drag there.")
        }
    }
    if kind == "key" || kind == "type", let expected = args["expect_pid"] {
        guard let pid = expected as? Int, pid > 0 else { throw invalid("expect_pid") }
        guard holdsKeyboard(pid) else {
            throw fail("refused", "The keyboard is not in the granted app now, so Pebble does not type there.")
        }
    }
}

func hex(_ text: String) -> [UInt8]? {
    guard text.utf8.count == 64, text.allSatisfy({ $0.isHexDigit && !$0.isUppercase }) else { return nil }
    return Array(text.utf8)
}

func takeLease(_ args: [String: Any]) throws -> [String: Any] {
    guard let token = (args["token"] as? String).flatMap(hex), let seconds = args["seconds"] as? Double,
          seconds > 0, seconds <= MAX_LEASE_SECONDS else { throw invalid("lease") }
    lease = (token, DispatchTime.now().uptimeNanoseconds + UInt64(seconds * 1e9))
    return ["leased": true, "seconds": seconds, "watching": startWatch()]
}

func liveLease(_ args: [String: Any]) -> Bool {
    guard let held = lease, DispatchTime.now().uptimeNanoseconds < held.until,
          let given = (args["lease"] as? String).flatMap(hex) else { return false }
    var difference: UInt8 = 0
    for (one, other) in zip(held.token, given) { difference |= one ^ other }
    return difference == 0
}

let KEYS: [String: Int] = [
    "return": kVK_Return, "tab": kVK_Tab, "space": kVK_Space, "delete": kVK_Delete, "forward_delete": kVK_ForwardDelete,
    "escape": kVK_Escape, "left": kVK_LeftArrow, "right": kVK_RightArrow, "up": kVK_UpArrow, "down": kVK_DownArrow,
    "home": kVK_Home, "end": kVK_End, "page_up": kVK_PageUp, "page_down": kVK_PageDown,
    "a": kVK_ANSI_A, "b": kVK_ANSI_B, "c": kVK_ANSI_C, "d": kVK_ANSI_D, "e": kVK_ANSI_E, "f": kVK_ANSI_F,
    "g": kVK_ANSI_G, "h": kVK_ANSI_H, "i": kVK_ANSI_I, "j": kVK_ANSI_J, "k": kVK_ANSI_K, "l": kVK_ANSI_L,
    "m": kVK_ANSI_M, "n": kVK_ANSI_N, "o": kVK_ANSI_O, "p": kVK_ANSI_P, "q": kVK_ANSI_Q, "r": kVK_ANSI_R,
    "s": kVK_ANSI_S, "t": kVK_ANSI_T, "u": kVK_ANSI_U, "v": kVK_ANSI_V, "w": kVK_ANSI_W, "x": kVK_ANSI_X,
    "y": kVK_ANSI_Y, "z": kVK_ANSI_Z, "0": kVK_ANSI_0, "1": kVK_ANSI_1, "2": kVK_ANSI_2, "3": kVK_ANSI_3,
    "4": kVK_ANSI_4, "5": kVK_ANSI_5, "6": kVK_ANSI_6, "7": kVK_ANSI_7, "8": kVK_ANSI_8, "9": kVK_ANSI_9,
    // F266: the punctuation keys of Mac shortcuts (⌘, ⌘. ⌘/ ⌘[ ⌘] ⌘= ⌘- ⌘` and, with shift, ⌘| ⌘{ ⌘} ⌘?)
    "minus": kVK_ANSI_Minus, "equal": kVK_ANSI_Equal, "left_bracket": kVK_ANSI_LeftBracket,
    "right_bracket": kVK_ANSI_RightBracket, "backslash": kVK_ANSI_Backslash, "semicolon": kVK_ANSI_Semicolon,
    "quote": kVK_ANSI_Quote, "comma": kVK_ANSI_Comma, "period": kVK_ANSI_Period, "slash": kVK_ANSI_Slash,
    "grave": kVK_ANSI_Grave]
let DRAG_STEPS = 16                 // F262: a drag's intermediate moves, about 20 ms apart (apps see a real drag)
let DRAG_STEP_MICROSECONDS: UInt32 = 20_000
let MODIFIERS: [String: CGEventFlags] = ["command": .maskCommand, "shift": .maskShift, "option": .maskAlternate,
                                         "control": .maskControl]

func point(_ args: [String: Any], _ name: String = "point") throws -> CGPoint {
    guard let pair = args[name] as? [Double], pair.count == 2,
          pair.allSatisfy({ $0.isFinite && abs($0) < 1e6 }) else {
        throw invalid(name)
    }
    return CGPoint(x: pair[0], y: pair[1])
}

/// Seconds since the person's last input: the tap's newest unmarked event, or the HID idle time before it saw one.
/// Phase 45: never negative (the tap thread may note an event after `now` was read; an unsigned underflow would trap).
func quietSeconds(_ now: UInt64) -> Double {
    let last = watch.last()
    if last == 0 {
        return CGEventSource.secondsSinceLastEventType(.hidSystemState, eventType: CGEventType(rawValue: ~0)!)
    }
    return now > last ? Double(now - last) / 1e9 : 0
}

/// Between events: stop when the person did anything since `began`, secure input came on, or the lease ended.
func stillClear(_ args: [String: Any], since began: UInt64) throws {
    if watch.last() > began { throw fail("interrupted", "The person used the keyboard or mouse; Pebble stopped.") }
    if IsSecureEventInputEnabled() { throw fail("interrupted", "A password field took the keyboard; Pebble stopped.") }
    if !liveLease(args) { throw fail("interrupted", "The input lease ended; Pebble stopped.") }
}

func input(_ args: [String: Any]) throws -> [String: Any] {
    guard liveLease(args) else { throw fail("refused", "No live input lease.") }
    guard CGPreflightPostEventAccess() else { throw fail("not_permitted", "Pebble may not post input events here.") }
    guard startWatch() else {
        throw fail("refused", "Pebble cannot notice your own keyboard and mouse here, so it does not use them.")
    }
    if IsSecureEventInputEnabled() { throw fail("refused", "A password field has the keyboard.") }
    let idle = args["idle_seconds"] as? Double ?? 1.0
    guard idle >= 0.2, idle <= 30 else { throw invalid("idle_seconds") }
    // Phase 45: wait_seconds (at most 10) waits that long for the person to be quiet before refusing, so an act the
    // person approved with a click in Pebble is not refused for that click. Nothing is posted while it waits.
    let patience = args["wait_seconds"] as? Double ?? 0
    guard patience >= 0, patience <= 10 else { throw invalid("wait_seconds") }
    var now = DispatchTime.now().uptimeNanoseconds
    let until = now + UInt64(patience * 1e9)
    while quietSeconds(now) < idle {
        guard now < until else { throw fail("refused", "The person is using the Mac; Pebble waits.") }
        usleep(100_000)
        now = DispatchTime.now().uptimeNanoseconds
    }
    if IsSecureEventInputEnabled() { throw fail("refused", "A password field has the keyboard.") }
    try targetHolds(args["kind"] as? String ?? "", args)
    let began = now
    var sent = 0
    defer { releaseAll() }
    switch args["kind"] as? String {
    case "click":
        let at = try point(args)
        let right = (args["button"] as? String) == "right"
        let count = args["count"] as? Int ?? 1
        guard (1...3).contains(count) else { throw invalid("count") }  // F261: 3 selects a line, as a triple-click
        let button: CGMouseButton = right ? .right : .left
        try post(CGEvent(mouseEventSource: source, mouseType: .mouseMoved, mouseCursorPosition: at,
                         mouseButton: button))
        for click in 1...count {
            try stillClear(args, since: began)
            let down = CGEvent(mouseEventSource: source, mouseType: right ? .rightMouseDown : .leftMouseDown,
                               mouseCursorPosition: at, mouseButton: button)
            down?.setIntegerValueField(.mouseEventClickState, value: Int64(click))
            heldButtons.insert(button.rawValue)
            try post(down)
            let up = CGEvent(mouseEventSource: source, mouseType: right ? .rightMouseUp : .leftMouseUp,
                             mouseCursorPosition: at, mouseButton: button)
            up?.setIntegerValueField(.mouseEventClickState, value: Int64(click))
            try post(up)
            heldButtons.remove(button.rawValue)
            sent += 2
        }
    case "drag":  // Phase 45 (F262): press at point, move in DRAG_STEPS steps to `to`, release there
        let from = try point(args), to = try point(args, "to")
        try post(CGEvent(mouseEventSource: source, mouseType: .mouseMoved, mouseCursorPosition: from,
                         mouseButton: .left))
        usleep(60_000)
        try stillClear(args, since: began)
        let down = CGEvent(mouseEventSource: source, mouseType: .leftMouseDown, mouseCursorPosition: from,
                           mouseButton: .left)
        down?.setIntegerValueField(.mouseEventClickState, value: 1)
        heldButtons.insert(CGMouseButton.left.rawValue)
        try post(down)
        for step in 1...DRAG_STEPS {
            usleep(DRAG_STEP_MICROSECONDS)
            try stillClear(args, since: began)
            let share = CGFloat(step) / CGFloat(DRAG_STEPS)
            let at = CGPoint(x: from.x + (to.x - from.x) * share, y: from.y + (to.y - from.y) * share)
            try post(CGEvent(mouseEventSource: source, mouseType: .leftMouseDragged, mouseCursorPosition: at,
                             mouseButton: .left))
        }
        usleep(60_000)
        let up = CGEvent(mouseEventSource: source, mouseType: .leftMouseUp, mouseCursorPosition: to, mouseButton: .left)
        up?.setIntegerValueField(.mouseEventClickState, value: 1)
        try post(up)
        heldButtons.remove(CGMouseButton.left.rawValue)
        sent += DRAG_STEPS + 3
    case "scroll":
        let at = try point(args)
        let dy = args["dy"] as? Int ?? 0, dx = args["dx"] as? Int ?? 0
        guard abs(dy) <= 50, abs(dx) <= 50, dy != 0 || dx != 0 else { throw invalid("scroll") }
        try post(CGEvent(mouseEventSource: source, mouseType: .mouseMoved, mouseCursorPosition: at, mouseButton: .left))
        try stillClear(args, since: began)
        try post(CGEvent(scrollWheelEvent2Source: source, units: .line, wheelCount: 2, wheel1: Int32(dy),
                         wheel2: Int32(dx), wheel3: 0))
        sent += 1
    case "key":
        guard let name = args["key"] as? String, let code = KEYS[name] else { throw invalid("key") }
        let names = args["modifiers"] as? [String] ?? []
        guard names.count <= 4, names.allSatisfy({ MODIFIERS[$0] != nil }) else { throw invalid("modifiers") }
        let flags = names.reduce(CGEventFlags()) { $0.union(MODIFIERS[$1]!) }
        try stillClear(args, since: began)
        let key = CGKeyCode(code)
        let before = CGEventSource.flagsState(.hidSystemState).intersection(HELD_FLAGS)
        let used = FN_CARRIERS.contains(code) ? flags.union(.maskSecondaryFn) : flags
        modifiersInAct = (before, used)
        let down = CGEvent(keyboardEventSource: source, virtualKey: key, keyDown: true)
        down?.flags = flags
        heldKeys.insert(key)
        try post(down)
        let up = CGEvent(keyboardEventSource: source, virtualKey: key, keyDown: false)
        up?.flags = flags
        try post(up)
        heldKeys.remove(key)
        sent += 2 + restoreModifiers(before, used: used)  // F258: the chord's modifiers (and an arrow's Fn) go up
        modifiersInAct = nil
    case "type":
        guard let text = args["text"] as? String, !text.isEmpty, text.count <= MAX_TYPED, !text.contains("\0") else {
            throw invalid("text")
        }
        for character in text {
            try stillClear(args, since: began)
            let units = Array(String(character).utf16)
            for down in [true, false] {
                let event = CGEvent(keyboardEventSource: source, virtualKey: 0, keyDown: down)
                units.withUnsafeBufferPointer { event?.keyboardSetUnicodeString(stringLength: units.count,
                                                                              unicodeString: $0.baseAddress) }
                try post(event)
            }
            sent += 2
        }
    default:
        throw invalid("kind")
    }
    return ["posted": sent]
}

// ---- the loop ------------------------------------------------------------------------------------------------------

func handle(_ line: String) -> [String: Any] {
    guard let data = line.data(using: .utf8),
          let request = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
          let id = request["id"] as? Int, id >= 0, let op = request["op"] as? String,
          let args = request["args"] as? [String: Any], Set(request.keys) == ["id", "op", "args"] else {
        return ["id": NSNull(), "ok": false, "error": "invalid", "detail": "request"]
    }
    do {
        let result: [String: Any]
        switch op {
        case "hello": result = hello()
        case "capture": result = try capture(args)
        case "ocr": result = try ocr(args)
        case "lease": result = try takeLease(args)
        case "release":
            lease = nil
            releaseAll()
            result = ["released": true]
        case "release_all":
            releaseAll()
            result = ["released": true, "lifted": releaseLeftovers()]
        case "input": result = try input(args)
        default: throw invalid("op")
        }
        return ["id": id, "ok": true, "result": result]
    } catch let error as Failure {
        return ["id": id, "ok": false, "error": error.code, "detail": String(error.detail.prefix(300))]
    } catch {
        return ["id": id, "ok": false, "error": "failed", "detail": String(describing: type(of: error))]
    }
}

// Requests, the lease, held keys and the signal handlers share one serial queue; the main queue stays free for
// ScreenCaptureKit's completions.
let work = DispatchQueue(label: "pebble-screen-helper")
var signals: [DispatchSourceSignal] = []
for number in [SIGTERM, SIGINT, SIGHUP] {
    signal(number, SIG_IGN)
    let handler = DispatchSource.makeSignalSource(signal: number, queue: work)
    handler.setEventHandler {
        releaseAll()
        exit(0)
    }
    handler.resume()
    signals.append(handler)
}

Thread {
    while let line = readLine(strippingNewline: true) {
        if line.utf8.count > MAX_REQUEST {
            work.sync { answer(["id": NSNull(), "ok": false, "error": "protocol", "detail": "request too large"]) }
            break
        }
        work.sync { answer(handle(line)) }
    }
    work.async {
        releaseAll()
        exit(0)
    }
}.start()
dispatchMain()
