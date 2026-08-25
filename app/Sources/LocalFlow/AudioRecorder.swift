import AVFoundation
import Foundation

/// Captures microphone audio and hands it out as 16 kHz mono Float32 --
/// the format both Parakeet and Whisper want.
///
/// The engine is prepared at launch but only *started* when you hold the key.
/// That costs ~50ms of start latency, and it is a deliberate trade: leaving the
/// input node running would keep macOS's orange microphone indicator lit all
/// day, which is an odd look for a tool whose entire pitch is that it is not
/// listening to you.
final class AudioRecorder {
    /// Called on a background queue with each converted chunk.
    var onChunk: (([Float]) -> Void)?

    private let engine = AVAudioEngine()
    private var converter: AVAudioConverter?
    private let targetFormat = AVAudioFormat(
        commonFormat: .pcmFormatFloat32,
        sampleRate: 16_000,
        channels: 1,
        interleaved: false
    )!
    private var isRunning = false

    func prepare() {
        engine.prepare()
    }

    /// Requests microphone access. The completion runs on the main queue.
    static func requestPermission(_ completion: @escaping (Bool) -> Void) {
        switch AVCaptureDevice.authorizationStatus(for: .audio) {
        case .authorized:
            completion(true)
        case .notDetermined:
            AVCaptureDevice.requestAccess(for: .audio) { granted in
                DispatchQueue.main.async { completion(granted) }
            }
        default:
            completion(false)
        }
    }

    func start() throws {
        guard !isRunning else { return }

        let input = engine.inputNode
        let inputFormat = input.outputFormat(forBus: 0)
        guard inputFormat.sampleRate > 0 else {
            throw RecorderError.noInputDevice
        }
        guard let converter = AVAudioConverter(from: inputFormat, to: targetFormat) else {
            throw RecorderError.cannotConvert(inputFormat.sampleRate)
        }
        self.converter = converter

        // 100ms of input per callback: small enough that the daemon receives
        // audio while you are still talking, large enough not to thrash.
        let bufferSize = AVAudioFrameCount(inputFormat.sampleRate / 10)
        input.installTap(onBus: 0, bufferSize: bufferSize, format: inputFormat) { [weak self] buffer, _ in
            self?.handle(buffer: buffer, using: converter)
        }

        try engine.start()
        isRunning = true
    }

    func stop() {
        guard isRunning else { return }
        engine.inputNode.removeTap(onBus: 0)
        engine.stop()
        converter = nil
        isRunning = false
    }

    private func handle(buffer: AVAudioPCMBuffer, using converter: AVAudioConverter) {
        let ratio = targetFormat.sampleRate / buffer.format.sampleRate
        let capacity = AVAudioFrameCount(Double(buffer.frameLength) * ratio) + 64
        guard let output = AVAudioPCMBuffer(pcmFormat: targetFormat, frameCapacity: capacity) else {
            return
        }

        // AVAudioConverter pulls input through this block. Handing it the same
        // buffer twice would duplicate audio, so the flag ensures one delivery.
        var delivered = false
        var error: NSError?
        converter.convert(to: output, error: &error) { _, status in
            if delivered {
                status.pointee = .noDataNow
                return nil
            }
            delivered = true
            status.pointee = .haveData
            return buffer
        }

        if let error {
            NSLog("LocalFlow: audio conversion failed: \(error.localizedDescription)")
            return
        }
        guard output.frameLength > 0, let channel = output.floatChannelData?[0] else { return }
        onChunk?(Array(UnsafeBufferPointer(start: channel, count: Int(output.frameLength))))
    }

    enum RecorderError: LocalizedError {
        case noInputDevice
        case cannotConvert(Double)

        var errorDescription: String? {
            switch self {
            case .noInputDevice:
                return "No microphone input device is available."
            case .cannotConvert(let rate):
                return "Cannot convert \(Int(rate)) Hz input to 16 kHz."
            }
        }
    }
}
