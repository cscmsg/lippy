import AppKit
import AVFoundation

final class AppDelegate: NSObject, NSApplicationDelegate {

    static let version = "0.1.0"

    private let recorder = AudioRecorder()
    private let hud = HUD()
    private var hotkey: HotkeyMonitor?
    private var statusItem: NSStatusItem?

    /// All daemon I/O happens here. The socket calls block, so none of this may
    /// touch the main thread -- a stalled daemon must never freeze the UI of
    /// whatever app the user is typing into.
    private let daemonQueue = DispatchQueue(label: "com.cscmsg.localflow.daemon")

    private let bufferLock = NSLock()
    private var buffered: [Float] = []

    private var recordingStart: Date?
    private var targetApp: String?
    private var hudTimer: Timer?
    private var lastTranscript = ""

    /// Holds shorter than this are a fumbled keypress, not a dictation.
    private let minimumHold: TimeInterval = 0.3

    private var socketPath: String {
        (NSHomeDirectory() as NSString)
            .appendingPathComponent("Library/Application Support/LocalFlow/flowd.sock")
    }

    // MARK: - Settings

    private var mode: String {
        get { UserDefaults.standard.string(forKey: "mode") ?? "polish" }
        set { UserDefaults.standard.set(newValue, forKey: "mode"); rebuildMenu() }
    }

    private var hotkeyChoice: HotkeyMonitor.Key {
        get {
            HotkeyMonitor.Key(rawValue: UserDefaults.standard.string(forKey: "hotkey") ?? "")
                ?? .rightOption
        }
        set {
            UserDefaults.standard.set(newValue.rawValue, forKey: "hotkey")
            installHotkey()
            rebuildMenu()
        }
    }

    // MARK: - Lifecycle

    func applicationDidFinishLaunching(_ notification: Notification) {
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        statusItem?.button?.image = NSImage(
            systemSymbolName: "mic", accessibilityDescription: "LocalFlow")
        rebuildMenu()

        recorder.prepare()
        recorder.onChunk = { [weak self] samples in
            guard let self else { return }
            self.bufferLock.lock()
            self.buffered.append(contentsOf: samples)
            self.bufferLock.unlock()
        }

        // Asked again on first use in beginRecording(). An accessory app's TCC
        // dialog can appear behind other windows and sit there unnoticed, so a
        // launch-time request alone is not enough to rely on.
        AudioRecorder.requestPermission { _ in }

        if !HotkeyMonitor.hasAccessibilityPermission {
            HotkeyMonitor.promptForAccessibility()
        }
        installHotkey()
    }

    func applicationWillTerminate(_ notification: Notification) {
        hotkey?.stop()
        recorder.stop()
    }

    private func installHotkey() {
        hotkey?.stop()
        let monitor = HotkeyMonitor(key: hotkeyChoice)
        monitor.onPress = { [weak self] in self?.beginRecording() }
        monitor.onRelease = { [weak self] in self?.endRecording() }
        monitor.onAbort = { [weak self] in self?.abortRecording() }
        monitor.start()
        hotkey = monitor
    }

    // MARK: - Recording

    private func beginRecording() {
        guard recordingStart == nil else { return }

        switch AVCaptureDevice.authorizationStatus(for: .audio) {
        case .authorized:
            break
        case .notDetermined:
            // Activating puts the permission dialog in front of the user. It
            // steals focus, which normally would break the paste target -- but
            // this branch is not going to paste anything anyway.
            NSApp.activate(ignoringOtherApps: true)
            AudioRecorder.requestPermission { [weak self] granted in
                self?.hud.show(granted
                    ? .done("Microphone granted — hold the key again")
                    : .failed("Microphone denied — System Settings › Privacy › Microphone"))
            }
            return
        default:
            hud.show(.failed("Microphone denied — System Settings › Privacy › Microphone"))
            NSWorkspace.shared.open(URL(
                string: "x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone")!)
            return
        }

        // Captured before the HUD appears, while the real target is still
        // frontmost -- it is what tells the polish pass whether this is going
        // into Slack or into Mail.
        targetApp = NSWorkspace.shared.frontmostApplication?.localizedName

        bufferLock.lock(); buffered.removeAll(); bufferLock.unlock()

        do {
            try recorder.start()
        } catch {
            hud.show(.failed(error.localizedDescription))
            return
        }

        recordingStart = Date()
        hud.show(.recording(seconds: 0))
        hudTimer = Timer.scheduledTimer(withTimeInterval: 0.1, repeats: true) { [weak self] _ in
            guard let start = self?.recordingStart else { return }
            self?.hud.show(.recording(seconds: Date().timeIntervalSince(start)))
        }
    }

    private func abortRecording() {
        guard recordingStart != nil else { return }
        teardownRecording()
        hud.hide()
    }

    private func endRecording() {
        guard let start = recordingStart else { return }
        let held = Date().timeIntervalSince(start)
        teardownRecording()

        guard held >= minimumHold else {
            hud.hide()
            return
        }

        bufferLock.lock()
        let samples = buffered
        buffered.removeAll()
        bufferLock.unlock()

        guard !samples.isEmpty else {
            hud.show(.failed("No audio captured"))
            return
        }

        hud.show(.thinking)
        let mode = self.mode
        let app = self.targetApp

        daemonQueue.async { [weak self] in
            guard let self else { return }
            let client = DaemonClient(socketPath: self.socketPath)
            do {
                try client.connect()
                defer { client.disconnect() }
                try client.startUtterance(mode: mode, app: app)
                // ~1s per message keeps any single JSON frame small.
                for chunk in stride(from: 0, to: samples.count, by: 16_000) {
                    try client.sendAudio(Array(samples[chunk..<min(chunk + 16_000, samples.count)]))
                }
                let result = try client.finish()
                DispatchQueue.main.async { self.deliver(result) }
            } catch {
                DispatchQueue.main.async {
                    self.hud.show(.failed(error.localizedDescription))
                }
            }
        }
    }

    private func teardownRecording() {
        recorder.stop()
        hudTimer?.invalidate()
        hudTimer = nil
        recordingStart = nil
    }

    private func deliver(_ result: DaemonClient.Result) {
        guard !result.text.isEmpty else {
            hud.show(.failed("No speech detected"))
            return
        }
        lastTranscript = result.text
        rebuildMenu()

        // The HUD is dismissed before pasting so it cannot be mistaken for the
        // frontmost window at the moment the keystroke is posted.
        hud.show(.done(result.text))
        TextInjector.insert(result.text)

        if !result.usedLLM, !result.fallbackReason.isEmpty {
            NSLog("LocalFlow: polish fell back (%@)", result.fallbackReason)
        }
    }

    // MARK: - Menu

    private func rebuildMenu() {
        let menu = NSMenu()

        let header = NSMenuItem(
            title: "LocalFlow \(Self.version) — hold \(hotkeyChoice.displayName)",
            action: nil, keyEquivalent: "")
        header.isEnabled = false
        menu.addItem(header)
        menu.addItem(.separator())

        let polished = NSMenuItem(title: "Polished", action: #selector(setPolished), keyEquivalent: "")
        polished.state = mode == "polish" ? .on : .off
        polished.target = self
        menu.addItem(polished)

        let verbatim = NSMenuItem(title: "Verbatim", action: #selector(setVerbatim), keyEquivalent: "")
        verbatim.state = mode == "raw" ? .on : .off
        verbatim.target = self
        menu.addItem(verbatim)

        menu.addItem(.separator())

        let hotkeyItem = NSMenuItem(title: "Hotkey", action: nil, keyEquivalent: "")
        let hotkeyMenu = NSMenu()
        for key in HotkeyMonitor.Key.allCases {
            let item = NSMenuItem(title: key.displayName, action: #selector(chooseHotkey(_:)), keyEquivalent: "")
            item.state = key == hotkeyChoice ? .on : .off
            item.representedObject = key.rawValue
            item.target = self
            hotkeyMenu.addItem(item)
        }
        hotkeyItem.submenu = hotkeyMenu
        menu.addItem(hotkeyItem)

        if !lastTranscript.isEmpty {
            let copyItem = NSMenuItem(
                title: "Copy Last Transcript", action: #selector(copyLast), keyEquivalent: "")
            copyItem.target = self
            menu.addItem(copyItem)
        }

        menu.addItem(.separator())

        if !HotkeyMonitor.hasAccessibilityPermission {
            let grant = NSMenuItem(
                title: "⚠︎ Grant Accessibility Permission…",
                action: #selector(grantAccessibility), keyEquivalent: "")
            grant.target = self
            menu.addItem(grant)
        }

        let checkItem = NSMenuItem(title: "Check Daemon", action: #selector(checkDaemon), keyEquivalent: "")
        checkItem.target = self
        menu.addItem(checkItem)

        let configItem = NSMenuItem(title: "Open Config File", action: #selector(openConfig), keyEquivalent: "")
        configItem.target = self
        menu.addItem(configItem)

        menu.addItem(.separator())
        let quit = NSMenuItem(title: "Quit LocalFlow", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        menu.addItem(quit)

        statusItem?.menu = menu
    }

    @objc private func setPolished() { mode = "polish" }
    @objc private func setVerbatim() { mode = "raw" }

    @objc private func chooseHotkey(_ sender: NSMenuItem) {
        guard let raw = sender.representedObject as? String,
              let key = HotkeyMonitor.Key(rawValue: raw) else { return }
        hotkeyChoice = key
    }

    @objc private func copyLast() {
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(lastTranscript, forType: .string)
    }

    @objc private func grantAccessibility() {
        HotkeyMonitor.promptForAccessibility()
        NSWorkspace.shared.open(URL(
            string: "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility")!)
    }

    @objc private func checkDaemon() {
        let path = socketPath
        daemonQueue.async { [weak self] in
            let alive = DaemonClient(socketPath: path).probe()
            DispatchQueue.main.async {
                self?.hud.show(alive ? .done("Daemon is ready") : .failed("Daemon is not running"))
            }
        }
    }

    @objc private func openConfig() {
        let path = (NSHomeDirectory() as NSString)
            .appendingPathComponent("Library/Application Support/LocalFlow/config.json")
        NSWorkspace.shared.open(URL(fileURLWithPath: path))
    }
}
