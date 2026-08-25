import Darwin
import Foundation

/// Talks to lippyd over a unix domain socket using newline-delimited JSON.
///
/// One connection per utterance. The daemon keeps its recording state per
/// connection, so a fresh socket is also a clean slate -- if anything goes
/// wrong mid-dictation, dropping the connection cannot leave stale audio
/// buffered on the other side.
final class DaemonClient {
    struct Result {
        let text: String
        let raw: String
        let asrMilliseconds: Int
        let polishMilliseconds: Int
        let usedLLM: Bool
        let fallbackReason: String
    }

    enum ClientError: LocalizedError {
        case cannotConnect(String)
        case socketClosed
        case daemon(String)

        var errorDescription: String? {
            switch self {
            case .cannotConnect(let path):
                return "Can't reach the Lippy daemon at \(path). Is it running?"
            case .socketClosed:
                return "The daemon closed the connection unexpectedly."
            case .daemon(let message):
                return message
            }
        }
    }

    private let socketPath: String
    private var fd: Int32 = -1
    private var readBuffer = Data()

    init(socketPath: String) {
        self.socketPath = socketPath
    }

    // MARK: - Connection

    func connect() throws {
        let handle = socket(AF_UNIX, SOCK_STREAM, 0)
        guard handle >= 0 else { throw ClientError.cannotConnect(socketPath) }

        var address = sockaddr_un()
        address.sun_family = sa_family_t(AF_UNIX)
        let pathBytes = Array(socketPath.utf8)
        // sun_path is a fixed 104-byte C array; refuse rather than truncate,
        // because a truncated path connects to the wrong thing or nothing.
        guard pathBytes.count < MemoryLayout.size(ofValue: address.sun_path) else {
            close(handle)
            throw ClientError.cannotConnect(socketPath)
        }
        withUnsafeMutableBytes(of: &address.sun_path) { raw in
            raw.copyBytes(from: pathBytes)
        }

        let size = socklen_t(MemoryLayout<sockaddr_un>.size)
        let status = withUnsafePointer(to: &address) { pointer in
            pointer.withMemoryRebound(to: sockaddr.self, capacity: 1) { rebound in
                Darwin.connect(handle, rebound, size)
            }
        }
        guard status == 0 else {
            close(handle)
            throw ClientError.cannotConnect(socketPath)
        }
        fd = handle
        readBuffer.removeAll()
    }

    func disconnect() {
        if fd >= 0 { close(fd) }
        fd = -1
    }

    // MARK: - Messages

    private func send(_ message: [String: Any]) throws {
        var data = try JSONSerialization.data(withJSONObject: message)
        data.append(0x0A)
        try data.withUnsafeBytes { raw in
            var offset = 0
            while offset < raw.count {
                let written = Darwin.write(fd, raw.baseAddress!.advanced(by: offset), raw.count - offset)
                if written <= 0 { throw ClientError.socketClosed }
                offset += written
            }
        }
    }

    private func receive() throws -> [String: Any] {
        while true {
            if let newline = readBuffer.firstIndex(of: 0x0A) {
                let line = readBuffer[..<newline]
                readBuffer.removeSubrange(...newline)
                if line.isEmpty { continue }
                guard let object = try JSONSerialization.jsonObject(with: line) as? [String: Any] else {
                    throw ClientError.socketClosed
                }
                if object["type"] as? String == "error" {
                    throw ClientError.daemon(object["message"] as? String ?? "unknown daemon error")
                }
                return object
            }
            var chunk = [UInt8](repeating: 0, count: 65536)
            let count = Darwin.read(fd, &chunk, chunk.count)
            guard count > 0 else { throw ClientError.socketClosed }
            readBuffer.append(contentsOf: chunk[..<count])
        }
    }

    // MARK: - Dictation

    func startUtterance(mode: String, app: String?) throws {
        var message: [String: Any] = ["type": "start", "mode": mode]
        if let app { message["app"] = app }
        try send(message)
        _ = try receive()  // "ready"
    }

    func sendAudio(_ samples: [Float]) throws {
        var pcm = Data(capacity: samples.count * 2)
        for sample in samples {
            let clamped = max(-1.0, min(1.0, sample))
            var value = Int16(clamped * 32767).littleEndian
            withUnsafeBytes(of: &value) { pcm.append(contentsOf: $0) }
        }
        try send(["type": "audio", "pcm": pcm.base64EncodedString()])
    }

    /// Sends the stop message and waits for the finished text.
    func finish() throws -> Result {
        try send(["type": "stop"])
        let response = try receive()
        return Result(
            text: response["text"] as? String ?? "",
            raw: response["raw"] as? String ?? "",
            asrMilliseconds: response["asr_ms"] as? Int ?? 0,
            polishMilliseconds: response["polish_ms"] as? Int ?? 0,
            usedLLM: response["used_llm"] as? Bool ?? false,
            fallbackReason: response["fallback_reason"] as? String ?? ""
        )
    }

    func cancel() {
        try? send(["type": "cancel"])
    }

    /// One-shot health check used by the menu bar.
    func probe() -> Bool {
        do {
            try connect()
            defer { disconnect() }
            try send(["type": "status"])
            return (try receive())["ready"] as? Bool ?? false
        } catch {
            return false
        }
    }
}
