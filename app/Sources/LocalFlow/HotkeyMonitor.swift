import AppKit
import Carbon.HIToolbox

/// Watches for press-and-hold on a modifier key, plus an optional latch chord.
///
/// Two ways to capture:
///   * **Hold** the primary key (default Right Option) -- records while held.
///   * **Primary + latch modifier** (default Right Shift) -- records until the
///     primary key is pressed again. For dictation too long to hold a key for.
///
/// Because pressing the chord necessarily involves pressing the primary key,
/// press order is handled explicitly: with the latch already down the capture
/// starts latched, and adding the latch mid-hold *promotes* the recording
/// already in progress rather than starting over. Either order works, and you
/// can decide a sentence in that this one is going long.
///
/// Requires Accessibility permission: without it macOS delivers no global key
/// events at all, and the app looks broken rather than unauthorised.
final class HotkeyMonitor {

    private enum DeviceMask {
        static let rightOption: UInt = 0x040
        static let leftOption: UInt = 0x020
        static let rightCommand: UInt = 0x010
        static let rightControl: UInt = 0x2000
        static let rightShift: UInt = 0x004
        static let leftShift: UInt = 0x002
    }

    enum Key: String, CaseIterable {
        case rightOption, leftOption, rightCommand, rightControl, rightShift, leftShift, fn

        var keyCode: UInt16 {
            switch self {
            case .rightOption: return UInt16(kVK_RightOption)
            case .leftOption: return UInt16(kVK_Option)
            case .rightCommand: return UInt16(kVK_RightCommand)
            case .rightControl: return UInt16(kVK_RightControl)
            case .rightShift: return UInt16(kVK_RightShift)
            case .leftShift: return UInt16(kVK_Shift)
            case .fn: return UInt16(kVK_Function)
            }
        }

        var deviceMask: UInt {
            switch self {
            case .rightOption: return DeviceMask.rightOption
            case .leftOption: return DeviceMask.leftOption
            case .rightCommand: return DeviceMask.rightCommand
            case .rightControl: return DeviceMask.rightControl
            case .rightShift: return DeviceMask.rightShift
            case .leftShift: return DeviceMask.leftShift
            case .fn: return UInt(NSEvent.ModifierFlags.function.rawValue)
            }
        }

        var displayName: String {
            switch self {
            case .rightOption: return "Right Option"
            case .leftOption: return "Left Option"
            case .rightCommand: return "Right Command"
            case .rightControl: return "Right Control"
            case .rightShift: return "Right Shift"
            case .leftShift: return "Left Shift"
            case .fn: return "Fn / Globe"
            }
        }
    }

    private enum State { case idle, holding, latched }

    /// Capture started. `latched` is true when it will run until stopped.
    var onBegin: ((Bool) -> Void)?
    /// A hold was upgraded to latched mid-recording. Audio so far is kept.
    var onPromote: (() -> Void)?
    /// Capture finished normally -- key released, or pressed again when latched.
    var onEnd: (() -> Void)?
    /// Another key was struck mid-hold: that was a chord, not dictation.
    var onAbort: (() -> Void)?

    private let key: Key
    private let latchKey: Key?
    private var state: State = .idle
    private var latchDown = false
    private var monitors: [Any] = []

    init(key: Key, latchKey: Key?) {
        self.key = key
        // Binding both actions to one key makes a press ambiguous.
        self.latchKey = (latchKey == key) ? nil : latchKey
    }

    static var hasAccessibilityPermission: Bool { AXIsProcessTrusted() }

    static func promptForAccessibility() {
        let options = [kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String: true]
        _ = AXIsProcessTrustedWithOptions(options as CFDictionary)
    }

    func start() {
        // Global monitors see other apps' events; local monitors see our own.
        // Both are needed, or the hotkey dies whenever a LocalFlow window is key.
        let global = NSEvent.addGlobalMonitorForEvents(matching: .flagsChanged) { [weak self] in
            self?.handleFlags($0)
        }
        let local = NSEvent.addLocalMonitorForEvents(matching: .flagsChanged) { [weak self] event in
            self?.handleFlags(event)
            return event
        }
        let keys = NSEvent.addGlobalMonitorForEvents(matching: [.keyDown]) { [weak self] _ in
            // Only a *hold* is abandoned this way. A latched session is
            // deliberate, and the user may well press keys during it.
            guard let self, self.state == .holding else { return }
            self.state = .idle
            self.onAbort?()
        }
        monitors = [global, local, keys].compactMap { $0 }
    }

    func stop() {
        for monitor in monitors { NSEvent.removeMonitor(monitor) }
        monitors = []
        state = .idle
        latchDown = false
    }

    /// Ends a latched capture from outside -- used by the runaway-session cap.
    func forceEnd() {
        guard state != .idle else { return }
        state = .idle
    }

    private func handleFlags(_ event: NSEvent) {
        if let latchKey, event.keyCode == latchKey.keyCode {
            latchDown = (event.modifierFlags.rawValue & latchKey.deviceMask) != 0
            if latchDown, state == .holding {
                state = .latched
                onPromote?()
            }
            return
        }

        guard event.keyCode == key.keyCode else { return }
        let isDown = (event.modifierFlags.rawValue & key.deviceMask) != 0

        if isDown {
            switch state {
            case .latched:
                // Second press ends a latched session. Checked first, so it
                // wins even if the latch modifier happens to be held again.
                state = .idle
                onEnd?()
            case .idle:
                state = latchDown ? .latched : .holding
                onBegin?(latchDown)
            case .holding:
                break
            }
        } else if state == .holding {
            state = .idle
            onEnd?()
        }
        // Releasing the primary key while latched is deliberately ignored.
    }
}
