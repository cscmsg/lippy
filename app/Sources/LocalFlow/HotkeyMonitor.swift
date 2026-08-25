import AppKit
import Carbon.HIToolbox

/// Watches for press-and-hold on a single modifier key.
///
/// Right-Option is the default because on a US layout it is the one modifier
/// nobody uses on its own -- and because Wispr Flow binds fn, so this coexists
/// with it rather than fighting it.
///
/// Requires Accessibility permission: without it, macOS delivers no global key
/// events at all and the app looks broken rather than unauthorised, so
/// `hasAccessibilityPermission` is checked before we ever claim to be ready.
final class HotkeyMonitor {
    /// Device-dependent modifier masks. `NSEvent.modifierFlags.option` cannot
    /// tell left from right; these bits can.
    private enum DeviceMask {
        static let rightOption: UInt = 0x040
        static let leftOption: UInt = 0x020
        static let rightCommand: UInt = 0x010
        static let rightControl: UInt = 0x2000
        static let rightShift: UInt = 0x004
    }

    enum Key: String, CaseIterable {
        case rightOption, leftOption, rightCommand, rightControl, rightShift, fn

        var keyCode: UInt16 {
            switch self {
            case .rightOption: return UInt16(kVK_RightOption)
            case .leftOption: return UInt16(kVK_Option)
            case .rightCommand: return UInt16(kVK_RightCommand)
            case .rightControl: return UInt16(kVK_RightControl)
            case .rightShift: return UInt16(kVK_RightShift)
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
            case .fn: return "Fn / Globe"
            }
        }
    }

    var onPress: (() -> Void)?
    var onRelease: (() -> Void)?
    /// Fired when another key is struck mid-recording, which means the hold was
    /// a modifier chord and not a dictation -- the recording is abandoned.
    var onAbort: (() -> Void)?

    private let key: Key
    private var flagsMonitors: [Any] = []
    private var keyMonitor: Any?
    private var isHeld = false

    init(key: Key) {
        self.key = key
    }

    static var hasAccessibilityPermission: Bool {
        AXIsProcessTrusted()
    }

    /// Opens the system prompt that lets the user grant Accessibility.
    static func promptForAccessibility() {
        let options = [kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String: true]
        _ = AXIsProcessTrustedWithOptions(options as CFDictionary)
    }

    func start() {
        // Global monitors see events destined for other apps; local monitors
        // see our own. We need both, or the hotkey dies whenever a LocalFlow
        // window (the HUD, a menu) happens to be key.
        let global = NSEvent.addGlobalMonitorForEvents(matching: .flagsChanged) { [weak self] event in
            self?.handleFlags(event)
        }
        let local = NSEvent.addLocalMonitorForEvents(matching: .flagsChanged) { [weak self] event in
            self?.handleFlags(event)
            return event
        }
        flagsMonitors = [global, local].compactMap { $0 }

        keyMonitor = NSEvent.addGlobalMonitorForEvents(matching: [.keyDown]) { [weak self] _ in
            guard let self, self.isHeld else { return }
            self.isHeld = false
            self.onAbort?()
        }
    }

    func stop() {
        for monitor in flagsMonitors { NSEvent.removeMonitor(monitor) }
        flagsMonitors = []
        if let keyMonitor { NSEvent.removeMonitor(keyMonitor) }
        keyMonitor = nil
    }

    private func handleFlags(_ event: NSEvent) {
        guard event.keyCode == key.keyCode else { return }
        let isDown = (event.modifierFlags.rawValue & key.deviceMask) != 0

        if isDown, !isHeld {
            isHeld = true
            onPress?()
        } else if !isDown, isHeld {
            isHeld = false
            onRelease?()
        }
    }
}
