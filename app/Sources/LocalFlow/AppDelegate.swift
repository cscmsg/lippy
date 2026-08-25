import AppKit
import AVFoundation

final class AppDelegate: NSObject, NSApplicationDelegate {

    static let version = "0.3.0"

    private let recorder = AudioRecorder()
    private let hud = HUD()
    private var hotkey: HotkeyMonitor?
    private var statusItem: NSStatusItem?

    /// All daemon I/O happens here. The socket calls block, so none of this may
    /// touch the main thread -- a stalled daemon must never freeze the UI of
    /// whatever app the user is typing into.
    private let daemonQueue = DispatchQueue(label: "com.cscmsg.localflow.daemon")

    /// Owned by daemonQueue. Never touch these from the main thread.
    private var client: DaemonClient?
    private var sessionActive = false

    private var partialText = ""
    private var recordingStart: Date?
    private var isLatched = false
    private var targetApp: String?
    private var hudTimer: Timer?
    private var latchCapTimer: Timer?
    private var lastTranscript = ""

    /// Holds shorter than this are a fumbled keypress. Applies to holds only:
    /// a latched session is deliberate however briefly it ran.
    private let minimumHold: TimeInterval = 0.3

    /// A latched session with no key held can be forgotten. Stop it rather than
    /// record the room indefinitely.
    private let latchCap: TimeInterval = 5 * 60

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
            // Never let one key mean two things.
            if latchChoice == newValue { latchChoice = nil }
            installHotkey()
            rebuildMenu()
        }
    }

    private var latchChoice: HotkeyMonitor.Key? {
        get {
            guard let raw = UserDefaults.standard.string(forKey: "latchKey") else {
                return .rightShift
            }
            return raw.isEmpty ? nil : HotkeyMonitor.Key(rawValue: raw)
        }
        set {
            UserDefaults.standard.set(newValue?.rawValue ?? "", forKey: "latchKey")
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

        Log.write("=== launch: LocalFlow \(Self.version) ===")
        Log.write("bundle        \(Bundle.main.bundlePath)")
        Log.write("pid           \(ProcessInfo.processInfo.processIdentifier), ppid \(getppid())")
        Log.write("accessibility \(AXIsProcessTrusted())")
        Log.write("mic status    \(Self.micStatusName())")

        // The hotkey goes in FIRST, before anything that touches audio.
        //
        // AppKit swallows an Objective-C exception thrown from this delegate
        // method: the app keeps running, but the rest of the method never
        // executes. With audio set up first, one throw from AVAudioEngine left
        // the app alive with no event monitors installed and every key dead,
        // which reads as "the app is broken" rather than "one call failed".
        // Ordering it this way means an audio failure costs audio, not the
        // entire interface.
        // Chunks arrive off the realtime thread while recording; hand each one
        // to the daemon so it can run the live preview.
        recorder.onChunk = { [weak self] chunk in
            guard let self else { return }
            self.daemonQueue.async {
                guard self.sessionActive, let client = self.client else { return }
                do {
                    try client.sendAudio(chunk)
                } catch {
                    Log.write("audio send failed: \(error.localizedDescription)")
                    self.sessionActive = false
                }
            }
        }

        installHotkey()
        Log.write("hotkey installed")

        if !HotkeyMonitor.hasAccessibilityPermission {
            HotkeyMonitor.promptForAccessibility()
        }

        AudioRecorder.requestPermission { granted in
            Log.write("requestAccess at launch: granted=\(granted), "
                      + "status now \(Self.micStatusName())")
        }
        Log.write("launch sequence complete")
    }

    func applicationWillTerminate(_ notification: Notification) {
        hotkey?.stop()
        _ = recorder.stop()
    }

    private func installHotkey() {
        hotkey?.stop()
        let monitor = HotkeyMonitor(key: hotkeyChoice, latchKey: latchChoice)
        monitor.onBegin = { [weak self] latched in self?.beginRecording(latched: latched) }
        monitor.onPromote = { [weak self] in self?.promoteToLatched() }
        monitor.onEnd = { [weak self] in self?.endRecording() }
        monitor.onAbort = { [weak self] in self?.abortRecording() }
        monitor.start()
        hotkey = monitor
        Log.write("hotkey: hold \(hotkeyChoice.displayName)"
                  + (latchChoice.map { ", latch + \($0.displayName)" } ?? ", no latch"))
    }

    static func micStatusName() -> String {
        switch AVCaptureDevice.authorizationStatus(for: .audio) {
        case .authorized: return "authorized"
        case .denied: return "denied"
        case .restricted: return "restricted"
        case .notDetermined: return "notDetermined"
        @unknown default: return "unknown"
        }
    }

    // MARK: - Recording

    private func beginRecording(latched: Bool) {
        guard recordingStart == nil else { return }
        Log.write("capture begin (latched=\(latched)); mic \(Self.micStatusName())")

        switch AVCaptureDevice.authorizationStatus(for: .audio) {
        case .authorized:
            break
        case .notDetermined:
            // Activating puts the permission dialog in front of the user. It
            // steals focus, which normally would break the paste target -- but
            // this branch is not going to paste anything anyway.
            Log.write("status notDetermined -> activating and requesting access")
            NSApp.activate(ignoringOtherApps: true)
            AudioRecorder.requestPermission { [weak self] granted in
                Log.write("requestAccess returned granted=\(granted)")
                self?.hud.show(granted
                    ? .done("Microphone granted — hold the key again")
                    : .failed("Microphone denied — System Settings › Privacy › Microphone"))
            }
            return
        default:
            Log.write("microphone \(Self.micStatusName()) -> opening System Settings")
            hud.show(.failed("Microphone denied — System Settings › Privacy › Microphone"))
            NSWorkspace.shared.open(URL(
                string: "x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone")!)
            return
        }

        // Captured before the HUD appears, while the real target is still
        // frontmost -- it is what tells the polish pass whether this is going
        // into Slack or into Mail.
        targetApp = NSWorkspace.shared.frontmostApplication?.localizedName

        do {
            try recorder.start()
            Log.write("recorder started")
        } catch {
            Log.write("recorder FAILED to start: \(error.localizedDescription)")
            hud.show(.failed(error.localizedDescription))
            return
        }

        isLatched = latched
        partialText = ""
        recordingStart = Date()
        openSession(mode: mode, app: targetApp)

        hud.show(.recording(seconds: 0, latched: latched, partial: ""))
        hudTimer = Timer.scheduledTimer(withTimeInterval: 0.1, repeats: true) { [weak self] _ in
            guard let self, let start = self.recordingStart else { return }
            self.hud.show(.recording(seconds: Date().timeIntervalSince(start),
                                     latched: self.isLatched, partial: self.partialText))
        }
        if latched { startLatchCap() }
    }

    // MARK: - Daemon session

    /// Opens the connection at the start of capture so audio can stream while
    /// you are still speaking.
    private func openSession(mode: String, app: String?) {
        daemonQueue.async { [weak self] in
            guard let self else { return }
            let client = DaemonClient(socketPath: self.socketPath)
            do {
                try client.connect()
                try client.startUtterance(mode: mode, app: app)
                self.client = client
                self.sessionActive = true
                client.startReader(
                    onPartial: { text in
                        DispatchQueue.main.async { self.partialText = text }
                    },
                    onResult: { outcome in
                        DispatchQueue.main.async { self.handleResult(outcome) }
                    })
            } catch {
                Log.write("daemon connect failed: \(error.localizedDescription)")
                self.sessionActive = false
                self.client = nil
                DispatchQueue.main.async {
                    self.hud.show(.failed(error.localizedDescription))
                }
            }
        }
    }

    private func closeSession(cancel: Bool = false) {
        daemonQueue.async { [weak self] in
            guard let self else { return }
            if cancel, self.sessionActive { self.client?.cancel() }
            self.sessionActive = false
            self.client?.disconnect()
            self.client = nil
        }
    }

    private func handleResult(_ outcome: Swift.Result<DaemonClient.Result, Error>) {
        closeSession()
        switch outcome {
        case .success(let result):
            deliver(result)
        case .failure(let error):
            Log.write("daemon error: \(error.localizedDescription)")
            hud.show(.failed(error.localizedDescription))
        }
    }

    /// The primary key was already held when the latch modifier arrived. Keep
    /// the audio recorded so far and simply stop waiting for the key to come up.
    private func promoteToLatched() {
        guard recordingStart != nil, !isLatched else { return }
        Log.write("promoted hold -> latched")
        isLatched = true
        startLatchCap()
    }

    private func startLatchCap() {
        latchCapTimer?.invalidate()
        latchCapTimer = Timer.scheduledTimer(withTimeInterval: latchCap, repeats: false) {
            [weak self] _ in
            guard let self, self.recordingStart != nil else { return }
            Log.write("latch cap reached (\(Int(self.latchCap))s) -> ending capture")
            self.hotkey?.forceEnd()
            self.endRecording()
        }
    }

    private func abortRecording() {
        guard recordingStart != nil else { return }
        Log.write("capture aborted (another key was pressed mid-hold)")
        _ = recorder.stop()
        closeSession(cancel: true)
        teardownRecording()
        hud.hide()
    }

    private func endRecording() {
        guard let start = recordingStart else { return }
        let held = Date().timeIntervalSince(start)
        let latched = isLatched
        // stop() must be read before teardown -- it is what returns the audio.
        let samples = recorder.stop()
        teardownRecording()

        guard latched || held >= minimumHold else {
            Log.write("ignored \(String(format: "%.2f", held))s hold (below minimum)")
            closeSession(cancel: true)
            hud.hide()
            return
        }

        Log.write("capture end after \(String(format: "%.2f", held))s (latched=\(latched))")
        hud.show(.thinking)

        // `samples` is only the tail -- everything before it already streamed.
        daemonQueue.async { [weak self] in
            guard let self else { return }
            guard self.sessionActive, let client = self.client else {
                DispatchQueue.main.async { self.hud.show(.failed("No daemon session")) }
                return
            }
            do {
                if !samples.isEmpty { try client.sendAudio(samples) }
                try client.requestStop()   // the result arrives on the reader thread
            } catch {
                Log.write("daemon error: \(error.localizedDescription)")
                self.sessionActive = false
                DispatchQueue.main.async {
                    self.hud.show(.failed(error.localizedDescription))
                }
            }
        }
    }

    private func teardownRecording() {
        hudTimer?.invalidate()
        hudTimer = nil
        latchCapTimer?.invalidate()
        latchCapTimer = nil
        recordingStart = nil
        isLatched = false
    }

    private func deliver(_ result: DaemonClient.Result) {
        guard !result.text.isEmpty else {
            Log.write("daemon returned no text (\(result.fallbackReason))")
            hud.show(.failed("No speech detected"))
            return
        }
        lastTranscript = result.text
        rebuildMenu()
        Log.write("delivered \(result.text.count) chars "
                  + "(asr \(result.asrMilliseconds)ms, polish \(result.polishMilliseconds)ms, "
                  + "llm=\(result.usedLLM))")

        hud.show(.done(result.text))
        TextInjector.insert(result.text)
    }

    // MARK: - Menu

    private func rebuildMenu() {
        let menu = NSMenu()

        var title = "LocalFlow \(Self.version) — hold \(hotkeyChoice.displayName)"
        if let latch = latchChoice {
            title += " · +\(latch.displayName) to latch"
        }
        let header = NSMenuItem(title: title, action: nil, keyEquivalent: "")
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

        let hotkeyItem = NSMenuItem(title: "Hold Key", action: nil, keyEquivalent: "")
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

        let latchItem = NSMenuItem(title: "Latch Modifier", action: nil, keyEquivalent: "")
        let latchMenu = NSMenu()
        let none = NSMenuItem(title: "None (hold only)", action: #selector(chooseLatch(_:)), keyEquivalent: "")
        none.state = latchChoice == nil ? .on : .off
        none.representedObject = ""
        none.target = self
        latchMenu.addItem(none)
        latchMenu.addItem(.separator())
        for key in HotkeyMonitor.Key.allCases where key != hotkeyChoice {
            let item = NSMenuItem(title: key.displayName, action: #selector(chooseLatch(_:)), keyEquivalent: "")
            item.state = key == latchChoice ? .on : .off
            item.representedObject = key.rawValue
            item.target = self
            latchMenu.addItem(item)
        }
        latchItem.submenu = latchMenu
        menu.addItem(latchItem)

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

        let logItem = NSMenuItem(title: "Open App Log", action: #selector(openLog), keyEquivalent: "")
        logItem.target = self
        menu.addItem(logItem)

        menu.addItem(.separator())
        menu.addItem(NSMenuItem(title: "Quit LocalFlow",
                                action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q"))

        statusItem?.menu = menu
    }

    @objc private func setPolished() { mode = "polish" }
    @objc private func setVerbatim() { mode = "raw" }

    @objc private func chooseHotkey(_ sender: NSMenuItem) {
        guard let raw = sender.representedObject as? String,
              let key = HotkeyMonitor.Key(rawValue: raw) else { return }
        hotkeyChoice = key
    }

    @objc private func chooseLatch(_ sender: NSMenuItem) {
        guard let raw = sender.representedObject as? String else { return }
        latchChoice = raw.isEmpty ? nil : HotkeyMonitor.Key(rawValue: raw)
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
        NSWorkspace.shared.open(URL(fileURLWithPath: (NSHomeDirectory() as NSString)
            .appendingPathComponent("Library/Application Support/LocalFlow/config.json")))
    }

    @objc private func openLog() {
        NSWorkspace.shared.open(URL(fileURLWithPath: (NSHomeDirectory() as NSString)
            .appendingPathComponent("Library/Application Support/LocalFlow/app.log")))
    }
}
