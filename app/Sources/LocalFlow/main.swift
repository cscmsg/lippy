import AppKit

let arguments = CommandLine.arguments

if arguments.contains("--selftest") {
    // Verifies the daemon round trip without a mic or any TCC permission.
    let index = arguments.firstIndex(of: "--selftest")!
    let path = arguments.count > index + 1 ? arguments[index + 1] : nil
    exit(SelfTest.run(path: path))
}

if arguments.contains("--version") {
    print("LocalFlow \(AppDelegate.version)")
    exit(0)
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
// .accessory: menu bar only, no Dock icon, and -- critically -- the app never
// becomes frontmost, so the window you are dictating into keeps focus.
app.setActivationPolicy(.accessory)
app.run()
