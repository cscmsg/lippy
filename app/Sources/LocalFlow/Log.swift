import Foundation

/// Appends to `~/Library/Application Support/LocalFlow/app.log`.
///
/// NSLog goes to the unified system log, which `log stream` reads -- and this
/// account is not an admin, so `log stream` is unavailable. Without a file the
/// app has no way to report what it saw, which turned a one-line permissions
/// bug into a guessing game.
enum Log {
    private static let url: URL = {
        let dir = URL(fileURLWithPath: NSHomeDirectory())
            .appendingPathComponent("Library/Application Support/LocalFlow")
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        return dir.appendingPathComponent("app.log")
    }()

    private static let queue = DispatchQueue(label: "com.cscmsg.localflow.log")

    private static let formatter: DateFormatter = {
        let f = DateFormatter()
        f.dateFormat = "HH:mm:ss.SSS"
        return f
    }()

    static func write(_ message: String) {
        queue.async {
            let line = "\(formatter.string(from: Date()))  \(message)\n"
            guard let data = line.data(using: .utf8) else { return }
            if let handle = try? FileHandle(forWritingTo: url) {
                defer { try? handle.close() }
                _ = try? handle.seekToEnd()
                try? handle.write(contentsOf: data)
            } else {
                try? data.write(to: url)
            }
        }
    }
}
