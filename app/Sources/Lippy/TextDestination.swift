import AppKit
import ApplicationServices

/// Answers: "is there somewhere for text to land right now?"
///
/// The failure this exists to prevent is dictating a long message into nothing.
/// Pasting into a window with no editable field silently discards the text --
/// ⌘V goes to whatever has focus, and if that is a file list or a web page,
/// nothing happens and the words are gone.
///
/// The opposite failure costs more, though, and this module has hit it: refusing
/// to paste into a field that would have accepted the text. Every answer here is
/// therefore weighted so that only a *confident* "no" holds anything back.
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

    // MARK: - Decision

    /// The whole decision, as a pure function of what the accessibility tree
    /// said. Separated from the reading below so it can be tested with no
    /// focused window, no permissions and no running app.
    ///
    /// - Parameters:
    ///   - focusStatus: result of asking the system for the focused element.
    ///   - valueSettable: whether that element's value can be written. This is
    ///     the most reliable signal of an editable field, and it works across
    ///     toolkits.
    ///   - role: the element's accessibility role, or nil if it did not publish
    ///     one.
    static func availability(focusStatus: AXError,
                             valueSettable: Bool,
                             role: String?) -> Availability {
        // An error is the LEAST informative answer available, and it used to be
        // treated as the most confident one. It is what an app that has not
        // built an accessibility tree returns while the cursor is blinking in a
        // text box, which is every Electron app until something asks it to build
        // one. Holding text back on an error meant "nowhere to paste" in a
        // window that would have taken the paste perfectly well.
        guard focusStatus == .success else { return .unknown }

        if valueSettable { return .editable }
        guard let role else { return .unknown }
        if editableRoles.contains(role) { return .editable }
        if ambiguousRoles.contains(role) { return .unknown }
        return .notEditable
    }

    // MARK: - Reading

    static func check() -> Availability {
        guard AXIsProcessTrusted() else { return .unknown }

        var focused: CFTypeRef?
        let status = AXUIElementCopyAttributeValue(
            AXUIElementCreateSystemWide(),
            kAXFocusedUIElementAttribute as CFString,
            &focused)

        guard status == .success, let raw = focused else {
            Log.write("focus unreadable in \(frontmostName()) "
                      + "(AXError \(status.rawValue)) -- treating as unknown")
            return availability(focusStatus: status, valueSettable: false, role: nil)
        }
        let element = raw as! AXUIElement

        var settable: DarwinBoolean = false
        let settableOK = AXUIElementIsAttributeSettable(
            element, kAXValueAttribute as CFString, &settable) == .success

        var roleValue: CFTypeRef?
        let role = AXUIElementCopyAttributeValue(
            element, kAXRoleAttribute as CFString, &roleValue) == .success
            ? roleValue as? String
            : nil

        let answer = availability(focusStatus: status,
                                  valueSettable: settableOK && settable.boolValue,
                                  role: role)
        if answer == .notEditable {
            Log.write("focus in \(frontmostName()) is \(role ?? "no role") -- not editable")
        }
        return answer
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

    // MARK: - Waking Electron

    /// Apps already asked for their accessibility tree, keyed by pid and launch
    /// date. The date matters because the case that broke this was an Electron
    /// app being restarted: the new process starts with accessibility off again
    /// and can come back on the same pid.
    private static var woken: [pid_t: Date] = [:]

    /// Asks the frontmost app to publish an accessibility tree, if it is the
    /// kind of app that does not do so on its own.
    ///
    /// Chromium leaves its tree unbuilt until an assistive app needs it, so an
    /// Electron window answers "nothing is focused" while the cursor sits in a
    /// text box. `AXManualAccessibility` is the documented way for an assistive
    /// app to ask for it, and it is the same switch VoiceOver flips implicitly.
    /// Setting it on a native app is an unsupported-attribute error and does
    /// nothing.
    ///
    /// Verified: Electron keeps accessibility "disabled by default" for
    /// performance and documents `AXManualAccessibility` for assistive apps --
    /// electronjs.org/docs/latest/tutorial/accessibility, 2026-08-26.
    ///
    /// Call this at the start of a capture, not at the end. The tree is built
    /// asynchronously, so asking when the key goes down leaves the length of an
    /// utterance for the answer to arrive.
    static func wakeFrontmostApp() {
        guard AXIsProcessTrusted(),
              let app = NSWorkspace.shared.frontmostApplication else { return }

        let pid = app.processIdentifier
        // A nil launch date is not cached: with nothing to tell one run of the
        // app from the next, asking again every time is the safe direction.
        if let launched = app.launchDate {
            if woken[pid] == launched { return }
            woken[pid] = launched
        }

        let axApp = AXUIElementCreateApplication(pid)
        // The accessibility default is six seconds. An app that is wedged would
        // otherwise hold this thread for all six with the microphone already
        // open and the user already speaking.
        AXUIElementSetMessagingTimeout(axApp, 0.5)

        let err = AXUIElementSetAttributeValue(
            axApp, "AXManualAccessibility" as CFString, kCFBooleanTrue)
        if err == .success {
            Log.write("asked \(app.localizedName ?? "pid \(pid)") to publish accessibility")
        }
    }

    private static func frontmostName() -> String {
        NSWorkspace.shared.frontmostApplication?.localizedName ?? "an unnamed app"
    }
}

// MARK: - Self test

extension TextDestination {
    /// `Lippy --selftest-destination`. Pure decision only, so it needs no
    /// daemon, no permissions and no focused window.
    static func runSelfTest() -> Int32 {
        // (focusStatus, valueSettable, role, expected, why)
        let cases: [(AXError, Bool, String?, Availability, String)] = [
            (.success, true, "AXTextField", .editable, "an ordinary text field"),
            (.success, true, nil, .editable, "settable value beats a missing role"),
            (.success, false, "AXTextArea", .editable, "a text area that does not report settable"),
            (.success, false, "AXComboBox", .editable, "a combo box"),
            (.success, false, "AXSearchField", .editable, "a search field"),
            (.success, false, "AXWebArea", .unknown, "a web view under-reports its fields"),
            (.success, false, "AXGroup", .unknown, "a container, not an answer"),
            (.success, false, "AXButton", .notEditable, "a button really is nowhere to paste"),
            (.success, false, "AXList", .notEditable, "a file list really is nowhere to paste"),
            (.success, false, nil, .unknown, "an element that publishes no role at all"),
            (.noValue, false, nil, .unknown,
             "the reported bug: an Electron app with no tree built yet"),
            (.cannotComplete, false, nil, .unknown, "an app that did not answer in time"),
            (.apiDisabled, false, nil, .unknown, "accessibility switched off underneath us"),
            (.failure, false, "AXTextField", .unknown,
             "a failed read is unknown even when a stale role came back"),
        ]

        var failures = 0
        for (status, settable, role, expected, why) in cases {
            let got = availability(focusStatus: status, valueSettable: settable, role: role)
            let ok = got == expected
            if !ok { failures += 1 }
            print("  \(ok ? "PASS" : "FAIL")  status \(status.rawValue), settable \(settable), "
                  + "role \(role ?? "nil") -> \(got)  (\(why))")
        }
        print("\n  \(cases.count - failures)/\(cases.count) passed")
        return failures == 0 ? 0 : 1
    }
}
