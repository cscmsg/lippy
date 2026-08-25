import AppKit
import Foundation
import ServiceManagement

/// Registers LocalFlow as a login item, so dictation is available from the
/// moment you log in without remembering to launch anything.
///
/// The daemon already starts on its own -- it is a LaunchAgent with RunAtLoad --
/// so this is the missing half.
///
/// The system, not a preference file, is the source of truth. A user can remove
/// a login item in System Settings at any time, and a cached boolean would then
/// show a tick next to something that is switched off.
enum LoginItem {

    enum State {
        case enabled
        case disabled
        /// Registered, but the user has switched it off in System Settings.
        /// Nothing the app can do about this: only the user can re-enable it.
        case requiresApproval
        case unavailable
    }

    static var state: State {
        switch SMAppService.mainApp.status {
        case .enabled: return .enabled
        case .notRegistered: return .disabled
        case .requiresApproval: return .requiresApproval
        case .notFound: return .unavailable
        @unknown default: return .unavailable
        }
    }

    /// Returns the state after the attempt, so callers can report what happened
    /// rather than assuming it worked.
    @discardableResult
    static func setEnabled(_ enabled: Bool) -> State {
        do {
            if enabled {
                try SMAppService.mainApp.register()
            } else {
                try SMAppService.mainApp.unregister()
            }
            Log.write("login item \(enabled ? "registered" : "unregistered")")
        } catch {
            Log.write("login item change failed: \(error.localizedDescription)")
        }
        return state
    }

    static func openLoginItemsSettings() {
        guard let url = URL(string: "x-apple.systempreferences:com.apple.LoginItems-Settings.extension")
        else { return }
        NSWorkspace.shared.open(url)
    }
}
