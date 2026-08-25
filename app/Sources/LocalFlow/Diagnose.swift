import AVFoundation
import AppKit

/// `LocalFlow --diagnose` -- prints every precondition the app depends on.
///
/// The failure modes here are all silent: without Accessibility, macOS delivers
/// no global key events and the hotkey simply never fires; without Microphone,
/// the engine starts and records zeroes. Neither produces an error, so both
/// look like "the app is broken". This turns them into a line of output.
enum Diagnose {

    static func run() -> Int32 {
        // Line-buffer stdout: piped to a file this is fully buffered by default,
        // so a crash mid-diagnosis discards every line already "printed" and the
        // tool reports nothing at all about where it got to.
        setvbuf(stdout, nil, _IOLBF, 0)
        var healthy = true

        func report(_ label: String, _ ok: Bool, _ detail: String) {
            print("  \(ok ? "✓" : "✗")  \(label.padding(toLength: 16, withPad: " ", startingAt: 0))\(detail)")
            if !ok { healthy = false }
        }

        // TCC answers about the *responsible* process, which for a binary run
        // from a shell is the terminal, not this app. Run that way, the
        // microphone line reports the terminal's access and can read "granted"
        // while the real app is denied. Launched by LaunchServices the parent
        // is launchd, and the answer is about LocalFlow itself.
        let guiLaunched = getppid() == 1
        print("LocalFlow \(AppDelegate.version)\n")
        if !guiLaunched {
            print("  ⚠︎  Run from a shell — the Microphone line below describes the")
            print("      launching process, not LocalFlow. Use the app's menu bar")
            print("      item for the truth about its own permissions.\n")
        }

        let trusted = AXIsProcessTrusted()
        report("Accessibility", trusted,
               trusted ? "granted"
                       : "DENIED — hotkey and paste will not work. "
                         + "System Settings → Privacy & Security → Accessibility")

        let mic = AVCaptureDevice.authorizationStatus(for: .audio)
        report("Microphone", mic == .authorized, {
            switch mic {
            case .authorized: return "granted"
            case .notDetermined: return "not yet requested — launch the app once"
            default: return "DENIED — System Settings → Privacy & Security → Microphone"
            }
        }())

        // Only probe the device once access is granted. Touching AVAudioEngine
        // before that triggers the TCC prompt, and a bare binary run from a
        // terminal has no Info.plist to raise one from -- so it hangs forever
        // instead of reporting anything. A diagnostic must never block.
        if mic == .authorized {
            // The engine must be held in a variable. `AVAudioEngine().inputNode`
            // releases the engine at the end of the expression while the node it
            // owns is still in use -- a use-after-free that segfaults here.
            let engine = AVAudioEngine()
            let inputFormat = engine.inputNode.outputFormat(forBus: 0)
            report("Input device", inputFormat.sampleRate > 0,
                   inputFormat.sampleRate > 0
                       ? "\(Int(inputFormat.sampleRate)) Hz, \(inputFormat.channelCount) ch"
                       : "no microphone found")
        } else {
            report("Input device", false, "not probed (microphone access required first)")
        }

        let socketPath = (NSHomeDirectory() as NSString)
            .appendingPathComponent("Library/Application Support/LocalFlow/flowd.sock")
        let daemonUp = DaemonClient(socketPath: socketPath).probe()
        report("Daemon", daemonUp,
               daemonUp ? "ready at \(socketPath)"
                        : "unreachable — run: make install-daemon")

        let key = HotkeyMonitor.Key(
            rawValue: UserDefaults.standard.string(forKey: "hotkey") ?? "") ?? .rightOption
        let mode = UserDefaults.standard.string(forKey: "mode") ?? "polish"
        report("Hotkey", true, "hold \(key.displayName)")
        report("Cleanup", true, mode)

        print("\n\(healthy ? "Ready to dictate." : "Not ready — see the ✗ lines above.")")
        return healthy ? 0 : 1
    }
}
