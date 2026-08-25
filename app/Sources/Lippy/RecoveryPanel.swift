import AppKit

/// Shown when a finished transcript has nowhere to land.
///
/// Dictating a long message into a window with no editable field would
/// otherwise lose it silently: ⌘V goes to whatever has focus, and if that
/// cannot take text, the words are simply gone. This holds them and offers a
/// copy button.
///
/// It stays until dismissed. An auto-hiding panel would defeat the point --
/// the case it exists for is finishing a long dictation and only then noticing
/// there was no target, which is exactly when you may have looked away.
final class RecoveryPanel {

    private var panel: NSPanel?
    private var text = ""
    private var onCopied: (() -> Void)?

    func show(_ transcript: String, onCopied: (() -> Void)? = nil) {
        self.text = transcript
        self.onCopied = onCopied

        panel?.orderOut(nil)
        let panel = build(transcript)
        self.panel = panel
        // orderFrontRegardless, not makeKeyAndOrderFront: the app must not
        // activate. Whatever the user clicks next should be their target, not us.
        panel.orderFrontRegardless()
    }

    func hide() {
        panel?.orderOut(nil)
        panel = nil
    }

    @objc private func copyTapped() {
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(text, forType: .string)
        Log.write("recovery: transcript copied to clipboard")
        onCopied?()
        hide()
    }

    @objc private func dismissTapped() {
        Log.write("recovery: dismissed without copying")
        hide()
    }

    private func build(_ transcript: String) -> NSPanel {
        let width: CGFloat = 460
        let panel = NSPanel(
            contentRect: NSRect(x: 0, y: 0, width: width, height: 160),
            // .nonactivatingPanel lets it take clicks without pulling the app
            // to the front, so the window you meant to dictate into keeps focus.
            styleMask: [.borderless, .nonactivatingPanel],
            backing: .buffered,
            defer: false)
        panel.isFloatingPanel = true
        panel.level = .statusBar
        panel.backgroundColor = .clear
        panel.isOpaque = false
        panel.hasShadow = true
        panel.collectionBehavior = [.canJoinAllSpaces, .stationary, .fullScreenAuxiliary]

        let background = NSVisualEffectView()
        background.material = .hudWindow
        background.blendingMode = .behindWindow
        background.state = .active
        background.wantsLayer = true
        background.layer?.cornerRadius = 12
        background.translatesAutoresizingMaskIntoConstraints = false

        let title = NSTextField(labelWithString: "Nowhere to paste — text held")
        title.font = .systemFont(ofSize: 13, weight: .semibold)
        title.translatesAutoresizingMaskIntoConstraints = false

        let body = NSTextField(wrappingLabelWithString: Self.preview(of: transcript))
        body.font = .systemFont(ofSize: 12)
        body.textColor = .secondaryLabelColor
        body.translatesAutoresizingMaskIntoConstraints = false

        let copy = NSButton(title: "Copy", target: self, action: #selector(copyTapped))
        copy.bezelStyle = .rounded
        copy.keyEquivalent = "\r"
        copy.translatesAutoresizingMaskIntoConstraints = false

        let dismiss = NSButton(title: "Dismiss", target: self, action: #selector(dismissTapped))
        dismiss.bezelStyle = .rounded
        dismiss.translatesAutoresizingMaskIntoConstraints = false

        panel.contentView = background
        [title, body, copy, dismiss].forEach(background.addSubview)

        NSLayoutConstraint.activate([
            title.topAnchor.constraint(equalTo: background.topAnchor, constant: 14),
            title.leadingAnchor.constraint(equalTo: background.leadingAnchor, constant: 16),
            title.trailingAnchor.constraint(equalTo: background.trailingAnchor, constant: -16),

            body.topAnchor.constraint(equalTo: title.bottomAnchor, constant: 8),
            body.leadingAnchor.constraint(equalTo: background.leadingAnchor, constant: 16),
            body.trailingAnchor.constraint(equalTo: background.trailingAnchor, constant: -16),

            copy.trailingAnchor.constraint(equalTo: background.trailingAnchor, constant: -16),
            copy.bottomAnchor.constraint(equalTo: background.bottomAnchor, constant: -14),
            dismiss.trailingAnchor.constraint(equalTo: copy.leadingAnchor, constant: -8),
            dismiss.bottomAnchor.constraint(equalTo: copy.bottomAnchor),
            body.bottomAnchor.constraint(lessThanOrEqualTo: copy.topAnchor, constant: -12),
        ])

        background.layoutSubtreeIfNeeded()
        let height = max(140, body.fittingSize.height + 100)
        if let screen = NSScreen.main {
            panel.setFrame(NSRect(x: screen.visibleFrame.midX - width / 2,
                                  y: screen.visibleFrame.minY + 90,
                                  width: width, height: height),
                           display: true)
        }
        return panel
    }

    /// Enough of the text to recognise it, not the whole thing.
    private static func preview(of text: String, limit: Int = 240) -> String {
        text.count <= limit ? text : String(text.prefix(limit)) + "…"
    }
}
