import AVFoundation
import Foundation

/// `Lippy --selftest [file.wav]` -- exercises the Swift side of the socket
/// protocol without needing a microphone, Accessibility permission, or a
/// keypress.
///
/// This exists because everything else in the app can be verified by reading
/// it, but the client protocol is the one part that is only exercised at the
/// exact moment dictation happens -- which is the worst possible time to find
/// out it is wrong.
enum SelfTest {

    static func run(path: String?) -> Int32 {
        let socketPath = (NSHomeDirectory() as NSString)
            .appendingPathComponent("Library/Application Support/Lippy/lippyd.sock")

        let samples: [Float]
        if let path {
            do {
                samples = try loadResampled(path: path)
                print("loaded \(String(format: "%.2f", Double(samples.count) / 16_000))s from \(path)")
            } catch {
                print("could not read \(path): \(error.localizedDescription)")
                return 1
            }
        } else {
            // A quiet tone: loud enough to clear the daemon's silence gate, so
            // the full ASR path runs rather than short-circuiting.
            samples = (0..<16_000).map { 0.05 * sin(2 * .pi * 440 * Float($0) / 16_000) }
            print("using 1.0s synthetic tone (no file given)")
        }

        let client = DaemonClient(socketPath: socketPath)
        let started = Date()
        do {
            try client.connect()
            defer { client.disconnect() }
            try client.startUtterance(mode: "polish", app: "SelfTest")
            for chunk in stride(from: 0, to: samples.count, by: 16_000) {
                try client.sendAudio(Array(samples[chunk..<min(chunk + 16_000, samples.count)]))
            }
            let result = try client.finish()
            let elapsed = Int(Date().timeIntervalSince(started) * 1000)

            print("round trip  \(elapsed)ms  (asr \(result.asrMilliseconds)ms, polish \(result.polishMilliseconds)ms)")
            print("used LLM    \(result.usedLLM)\(result.fallbackReason.isEmpty ? "" : "  (\(result.fallbackReason))")")
            print("raw         \(result.raw.isEmpty ? "<empty>" : result.raw)")
            print("final       \(result.text.isEmpty ? "<empty>" : result.text)")
            print("\nOK — the Swift client speaks the daemon's protocol correctly.")
            return 0
        } catch {
            print("FAILED: \(error.localizedDescription)")
            return 1
        }
    }

    /// Reads any file AVFoundation can open and returns 16 kHz mono Float32.
    private static func loadResampled(path: String) throws -> [Float] {
        let file = try AVAudioFile(forReading: URL(fileURLWithPath: path))
        let target = AVAudioFormat(commonFormat: .pcmFormatFloat32,
                                   sampleRate: 16_000, channels: 1, interleaved: false)!
        guard let converter = AVAudioConverter(from: file.processingFormat, to: target),
              let input = AVAudioPCMBuffer(pcmFormat: file.processingFormat,
                                           frameCapacity: AVAudioFrameCount(file.length))
        else { throw SelfTestError.unreadable }

        try file.read(into: input)
        let ratio = target.sampleRate / file.processingFormat.sampleRate
        let capacity = AVAudioFrameCount(Double(input.frameLength) * ratio) + 1024
        guard let output = AVAudioPCMBuffer(pcmFormat: target, frameCapacity: capacity) else {
            throw SelfTestError.unreadable
        }

        var delivered = false
        var error: NSError?
        converter.convert(to: output, error: &error) { _, status in
            if delivered { status.pointee = .noDataNow; return nil }
            delivered = true
            status.pointee = .haveData
            return input
        }
        if let error { throw error }
        guard let channel = output.floatChannelData?[0] else { throw SelfTestError.unreadable }
        return Array(UnsafeBufferPointer(start: channel, count: Int(output.frameLength)))
    }

    enum SelfTestError: LocalizedError {
        case unreadable
        var errorDescription: String? { "Could not decode the audio file." }
    }
}
