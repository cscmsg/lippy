import AppKit
import ApplicationServices

/// Decides whether inserted text needs a space in front of it.
///
/// Dictation arrives one utterance at a time and is pasted at the cursor, which
/// knows nothing about what is already there. Two sentences in a row therefore
/// run together: "Ship it on Tuesday.The bug is fixed."
///
/// Always prepending a space is not the fix. It leaves a stray space in every
/// empty field, at the start of every line, and after every opening bracket. The
/// decision has to be made from what actually precedes the insertion point.
enum Separator {

    /// Characters after which a space would be wrong.
    private static let openers: Set<Character> = ["(", "[", "{", "<", "\"", "'", "\u{201C}", "\u{2018}", "/", "-", "\u{2013}", "\u{2014}", "@", "#", "$"]

    /// Pure decision, so it can be tested without a running app.
    ///
    /// - Parameters:
    ///   - preceding: the character immediately before the cursor, or nil when
    ///     the field is empty or the app does not expose its contents.
    ///   - inserting: the text about to be pasted.
    static func needed(after preceding: Character?, inserting: String) -> Bool {
        guard let preceding else { return false }
        guard let first = inserting.first else { return false }

        // Already separated, or at the start of a line.
        if preceding.isWhitespace || preceding.isNewline { return false }
        // A space after an opening bracket or quote is wrong.
        if openers.contains(preceding) { return false }
        // Text that opens with its own punctuation supplies its own spacing.
        if first.isWhitespace || first.isNewline { return false }
        if first.isPunctuation, !openers.contains(first) { return false }

        return true
    }

    /// Reads the character immediately before the insertion point in the focused
    /// field. Returns nil when nothing has focus, when the app does not publish
    /// its text, or when the cursor is at the very start.
    static func characterBeforeCursor() -> Character? {
        guard AXIsProcessTrusted() else { return nil }

        var focused: CFTypeRef?
        guard AXUIElementCopyAttributeValue(
                AXUIElementCreateSystemWide(),
                kAXFocusedUIElementAttribute as CFString, &focused) == .success,
              let raw = focused else { return nil }
        let element = raw as! AXUIElement

        var valueRef: CFTypeRef?
        guard AXUIElementCopyAttributeValue(
                element, kAXValueAttribute as CFString, &valueRef) == .success,
              let text = valueRef as? String, !text.isEmpty else { return nil }

        var rangeRef: CFTypeRef?
        guard AXUIElementCopyAttributeValue(
                element, kAXSelectedTextRangeAttribute as CFString, &rangeRef) == .success
        else { return nil }

        var range = CFRange()
        guard AXValueGetValue(rangeRef as! AXValue, .cfRange, &range) else { return nil }

        // Accessibility ranges are UTF-16 offsets, so the string has to be
        // indexed the same way. Using Character offsets would land in the wrong
        // place after any emoji or combining mark.
        let units = Array(text.utf16)
        let location = range.location
        guard location > 0, location <= units.count else { return nil }
        guard let scalar = Unicode.Scalar(units[location - 1]) else { return nil }
        return Character(scalar)
    }

    /// The text to paste, with a leading space when one is needed.
    ///
    /// Fails closed: when the focused app does not publish its contents, no
    /// space is added. A missing space between two utterances is a visible
    /// nuisance the user can fix in one keystroke. A spurious leading space
    /// appears on the *first* dictation into every such app, which is worse.
    static func prepare(_ text: String) -> String {
        needed(after: characterBeforeCursor(), inserting: text) ? " " + text : text
    }
}

// MARK: - Self test

extension Separator {
    /// `Lippy --selftest-separator`. Needs no daemon, no permissions and no
    /// focused window, so it runs anywhere including CI.
    static func runSelfTest() -> Int32 {
        // (preceding, inserting, expected, why)
        let cases: [(Character?, String, Bool, String)] = [
            (nil, "Hello", false, "empty field, or an app that does not publish its text"),
            (".", "How are you", true, "the reported bug: sentence hard against the last full stop"),
            ("d", "How are you", true, "mid-sentence continuation still needs a space"),
            ("?", "Yes", true, "after a question mark"),
            (" ", "Hello", false, "already separated"),
            ("\n", "Hello", false, "start of a line"),
            ("\t", "Hello", false, "after a tab"),
            ("(", "Hello", false, "opening bracket"),
            ("\"", "Hello", false, "opening quote"),
            ("-", "Hello", false, "hyphen, likely mid-word"),
            ("/", "Hello", false, "path or URL"),
            ("@", "Hello", false, "handle or address"),
            (".", ", and then", false, "inserted text opens with its own punctuation"),
            (".", " already spaced", false, "inserted text supplies its own space"),
            (".", "\"Quoted\"", true, "an opening quote is text, not punctuation to hug"),
            (".", "", false, "nothing to insert"),
        ]

        var failures = 0
        for (preceding, inserting, expected, why) in cases {
            let got = needed(after: preceding, inserting: inserting)
            let ok = got == expected
            if !ok { failures += 1 }
            let shown = preceding.map { String($0).debugDescription } ?? "nil"
            print("  \(ok ? "PASS" : "FAIL")  after \(shown) inserting \(inserting.debugDescription) -> \(got)  (\(why))")
        }
        print("\n  \(cases.count - failures)/\(cases.count) passed")
        return failures == 0 ? 0 : 1
    }
}
