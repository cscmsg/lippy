import AppKit

/// The small floating readout that shows what the app is doing.
///
/// It is an `NSPanel` with `.nonactivatingPanel` and `ignoresMouseEvents`
/// because it must never take focus: the whole operation depends on the app you
/// were typing into still being frontmost when the paste lands.
final class HUD {
    enum State {
        case recording(seconds: TimeInterval, latched: Bool, noDestination: Bool)
        case thinking
        case done(String)
        case failed(String)
    }

    private var panel: NSPanel?
    private let label = NSTextField(labelWithString: "")
    private let indicator = NSView()
    private var hideWorkItem: DispatchWorkItem?

    func show(_ state: State) {
        hideWorkItem?.cancel()
        let panel = panel ?? makePanel()
        self.panel = panel

        switch state {
        case .recording(let seconds, let latched, let noDestination):
            // Amber while there is nowhere for the text to land: better to
            // learn that now than after speaking for a minute.
            indicator.layer?.backgroundColor =
                (noDestination ? NSColor.systemOrange : NSColor.systemRed).cgColor
            // A latched session keeps running with no key held, so the HUD has
            // to say so unmistakably -- otherwise a forgotten session records
            // in silence and the user has no idea it is still listening.
            var line = latched
                ? String(format: "Listening (latched)  %.1fs  ·  press again to stop", seconds)
                : String(format: "Listening  %.1fs", seconds)
            if noDestination { line += "  ·  ⚠︎ no text field focused" }
            label.stringValue = line

        case .thinking:
            indicator.layer?.backgroundColor = NSColor.systemOrange.cgColor
            label.stringValue = "Polishing…"
        case .done(let text):
            indicator.layer?.backgroundColor = NSColor.systemGreen.cgColor
            label.stringValue = text
            scheduleHide(after: 1.2)
        case .failed(let message):
            indicator.layer?.backgroundColor = NSColor.systemGray.cgColor
            label.stringValue = message
            scheduleHide(after: 3.0)
        }

        label.sizeToFit()
        resize(panel)
        panel.orderFrontRegardless()
    }

    func hide() {
        hideWorkItem?.cancel()
        panel?.orderOut(nil)
    }

    private func scheduleHide(after delay: TimeInterval) {
        let work = DispatchWorkItem { [weak self] in self?.panel?.orderOut(nil) }
        hideWorkItem = work
        DispatchQueue.main.asyncAfter(deadline: .now() + delay, execute: work)
    }

    private func makePanel() -> NSPanel {
        let panel = NSPanel(
            contentRect: NSRect(x: 0, y: 0, width: 220, height: 40),
            styleMask: [.borderless, .nonactivatingPanel],
            backing: .buffered,
            defer: false
        )
        panel.isFloatingPanel = true
        panel.level = .statusBar
        panel.backgroundColor = .clear
        panel.isOpaque = false
        panel.hasShadow = true
        panel.ignoresMouseEvents = true
        panel.collectionBehavior = [.canJoinAllSpaces, .stationary, .fullScreenAuxiliary]

        let background = NSVisualEffectView()
        background.material = .hudWindow
        background.blendingMode = .behindWindow
        background.state = .active
        background.wantsLayer = true
        background.layer?.cornerRadius = 12
        background.translatesAutoresizingMaskIntoConstraints = false

        indicator.wantsLayer = true
        indicator.layer?.cornerRadius = 5
        indicator.translatesAutoresizingMaskIntoConstraints = false

        label.font = .systemFont(ofSize: 13, weight: .medium)
        label.textColor = .labelColor
        label.lineBreakMode = .byTruncatingTail
        label.translatesAutoresizingMaskIntoConstraints = false

        panel.contentView = background
        background.addSubview(indicator)
        background.addSubview(label)

        NSLayoutConstraint.activate([
            indicator.leadingAnchor.constraint(equalTo: background.leadingAnchor, constant: 14),
            indicator.centerYAnchor.constraint(equalTo: background.centerYAnchor),
            indicator.widthAnchor.constraint(equalToConstant: 10),
            indicator.heightAnchor.constraint(equalToConstant: 10),
            label.leadingAnchor.constraint(equalTo: indicator.trailingAnchor, constant: 10),
            label.trailingAnchor.constraint(lessThanOrEqualTo: background.trailingAnchor, constant: -14),
            label.centerYAnchor.constraint(equalTo: background.centerYAnchor),
        ])
        return panel
    }

    /// Sits just above the Dock, centred -- out of the way of whatever you are
    /// dictating into, which is usually near the middle of the screen.
    private func resize(_ panel: NSPanel) {
        guard let screen = NSScreen.main else { return }
        let width = min(max(label.frame.width + 60, 200), 520)
        let frame = NSRect(
            x: screen.visibleFrame.midX - width / 2,
            y: screen.visibleFrame.minY + 90,
            width: width,
            height: 40
        )
        panel.setFrame(frame, display: true)
    }
}
