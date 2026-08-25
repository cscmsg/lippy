import AppKit
import ApplicationServices

/// Answers: "is there somewhere for text to land right now?"
///
/// The failure this exists to prevent is dictating a long message into nothing.
/// Pasting into a window with no editable field silently discards the text --
/// ⌘V goes to whatever has focus, and if that is a file list or a web page,
/// nothing happens and the words are gone.
enum TextDestination {

    enum Availability {
        /// A text field, text area or similar has focus.
        case editable
        /// Focus is on something that cannot accept text, or nothing at all.
        case notEditable
        /// The accessibility tree did not give a usable answer.
        case unknown
    }

    /// Roles that reliably accept typed text.
    private static let editableRoles: Set<String> = [
        "AXTextField", "AXTextArea", "AXComboBox", "AXSearchField",
    ]

    /// Roles that are containers rather than answers -- common in browsers and
    /// Electron apps, where the real target is nested and under-reported.
    private static let ambiguousRoles: Set<String> = [
        "AXWebArea", "AXGroup", "AXScrollArea", "AXUnknown", "AXApplication", "AXWindow",
    ]

    static func check() -> Availability {
        guard AXIsProcessTrusted() else { return .unknown }

        var focused: CFTypeRef?
        let status = AXUIElementCopyAttributeValue(
            AXUIElementCreateSystemWide(),
            kAXFocusedUIElementAttribute as CFString,
            &focused)

        // No focused element at all: the Desktop, a Finder window, a dialog with
        // only buttons. There is nowhere for text to go.
        guard status == .success, let raw = focused else { return .notEditable }
        let element = raw as! AXUIElement

        // A settable value attribute is the most reliable signal of an editable
        // field, and it works across toolkits.
        var settable: DarwinBoolean = false
        if AXUIElementIsAttributeSettable(element, kAXValueAttribute as CFString, &settable)
            == .success, settable.boolValue {
            return .editable
        }

        var roleValue: CFTypeRef?
        guard AXUIElementCopyAttributeValue(element, kAXRoleAttribute as CFString, &roleValue)
                == .success, let role = roleValue as? String else {
            return .unknown
        }
        if editableRoles.contains(role) { return .editable }
        if ambiguousRoles.contains(role) { return .unknown }
        return .notEditable
    }

    /// Whether to hold text back rather than paste it.
    ///
    /// Only a confident `.notEditable` holds it back. `.unknown` pastes, because
    /// the accessibility tree under-reports editability in browsers and Electron
    /// apps, and wrongly withholding text from a field that would have accepted
    /// it is a worse failure than a paste that lands somewhere harmless.
    static var shouldHoldBack: Bool {
        check() == .notEditable
    }
}
