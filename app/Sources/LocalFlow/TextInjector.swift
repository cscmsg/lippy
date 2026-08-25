import AppKit
import Carbon.HIToolbox

/// Puts finished text into whatever app has focus.
///
/// Synthesised ⌘V rather than synthesised keystrokes: typing a 200-character
/// paragraph one CGEvent at a time takes seconds and drops characters in apps
/// that debounce input. Paste is instant and atomic.
///
/// The cost of paste is that it borrows the clipboard, so the previous contents
/// are saved and put back. That restore is best-effort by nature -- the
/// clipboard is global mutable state -- but it covers the case that actually
/// bites, which is losing something you copied a moment ago.
enum TextInjector {

    /// How long to let the target app read the pasteboard before restoring it.
    /// Below ~120ms, slower apps (Electron, JetBrains) paste the *restored*
    /// contents instead of the dictation.
    private static let restoreDelay: TimeInterval = 0.25

    static func insert(_ text: String) {
        guard !text.isEmpty else { return }

        let pasteboard = NSPasteboard.general
        let saved = snapshot(pasteboard)

        pasteboard.clearContents()
        pasteboard.setString(text, forType: .string)

        pressCommandV()

        DispatchQueue.main.asyncAfter(deadline: .now() + restoreDelay) {
            restore(saved, to: pasteboard)
        }
    }

    // MARK: - Clipboard preservation

    private struct Snapshot {
        let items: [[NSPasteboard.PasteboardType: Data]]
    }

    private static func snapshot(_ pasteboard: NSPasteboard) -> Snapshot {
        let items = (pasteboard.pasteboardItems ?? []).map { item in
            var stored: [NSPasteboard.PasteboardType: Data] = [:]
            for type in item.types {
                if let data = item.data(forType: type) {
                    stored[type] = data
                }
            }
            return stored
        }
        return Snapshot(items: items)
    }

    private static func restore(_ snapshot: Snapshot, to pasteboard: NSPasteboard) {
        pasteboard.clearContents()
        guard !snapshot.items.isEmpty else { return }
        let restored = snapshot.items.map { stored -> NSPasteboardItem in
            let item = NSPasteboardItem()
            for (type, data) in stored {
                item.setData(data, forType: type)
            }
            return item
        }
        pasteboard.writeObjects(restored)
    }

    // MARK: - Synthetic keystroke

    private static func pressCommandV() {
        // .privateState keeps the physical keyboard's real modifier state out
        // of these events. Without it, a Shift still held from the last
        // keystroke turns ⌘V into ⌘⇧V -- paste-and-match-style in some apps,
        // nothing at all in others.
        guard let source = CGEventSource(stateID: .privateState) else { return }
        let v = CGKeyCode(kVK_ANSI_V)

        guard let keyDown = CGEvent(keyboardEventSource: source, virtualKey: v, keyDown: true),
              let keyUp = CGEvent(keyboardEventSource: source, virtualKey: v, keyDown: false)
        else { return }

        keyDown.flags = .maskCommand
        keyUp.flags = .maskCommand
        keyDown.post(tap: .cghidEventTap)
        keyUp.post(tap: .cghidEventTap)
    }
}
