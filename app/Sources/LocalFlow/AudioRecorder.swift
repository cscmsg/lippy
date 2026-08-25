import AVFoundation
import Foundation

/// Captures microphone audio and returns it as 16 kHz mono Float32 --
/// the format both Parakeet and Whisper want.
///
/// The tap runs at the hardware's native format and does nothing but copy
/// floats; the conversion happens once, on stop, off the realtime thread.
///
/// An earlier design converted every tap buffer with an AVAudioConverter and
/// captured *nothing*: a 48 kHz -> 16 kHz resampler needs several buffers
/// before it can emit anything, and an input block that supplies one buffer and
/// then reports `.noDataNow` makes it return zero frames forever. It failed
/// silently -- engine running, tap firing, every converted buffer empty.
final class AudioRecorder {

    static let targetSampleRate: Double = 16_000

    private let engine = AVAudioEngine()
    private let lock = NSLock()
    private var captured: [Float] = []
    private var nativeSampleRate: Double = 0
    private var tapCallbacks = 0
    private var isRunning = false

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

        lock.lock(); captured.removeAll(); tapCallbacks = 0; lock.unlock()

        let input = engine.inputNode
        let format = input.inputFormat(forBus: 0)
        guard format.sampleRate > 0, format.channelCount > 0 else {
            throw RecorderError.noInputDevice
        }
        nativeSampleRate = format.sampleRate
        Log.write("input format \(Int(format.sampleRate)) Hz, \(format.channelCount) ch, "
                  + "interleaved=\(format.isInterleaved)")

        input.installTap(onBus: 0, bufferSize: 4096, format: format) { [weak self] buffer, _ in
            self?.append(buffer)
        }

        engine.prepare()
        try engine.start()
        isRunning = true
    }

    /// Stops capture and returns everything recorded, as 16 kHz mono.
    @discardableResult
    func stop() -> [Float] {
        guard isRunning else { return [] }
        engine.inputNode.removeTap(onBus: 0)
        engine.stop()
        isRunning = false

        lock.lock()
        let native = captured
        let callbacks = tapCallbacks
        captured.removeAll()
        lock.unlock()

        let resampled = Self.resample(native, from: nativeSampleRate)
        Log.write("captured \(callbacks) buffers, \(native.count) native samples "
                  + "@\(Int(nativeSampleRate))Hz -> \(resampled.count) @16kHz")
        return resampled
    }

    // MARK: - Capture

    private func append(_ buffer: AVAudioPCMBuffer) {
        guard let channels = buffer.floatChannelData else { return }
        let frames = Int(buffer.frameLength)
        guard frames > 0 else { return }
        let channelCount = Int(buffer.format.channelCount)

        var mono = [Float](repeating: 0, count: frames)
        if buffer.format.isInterleaved {
            let data = channels[0]
            for frame in 0..<frames {
                var sum: Float = 0
                for channel in 0..<channelCount { sum += data[frame * channelCount + channel] }
                mono[frame] = sum / Float(channelCount)
            }
        } else {
            for frame in 0..<frames {
                var sum: Float = 0
                for channel in 0..<channelCount { sum += channels[channel][frame] }
                mono[frame] = sum / Float(channelCount)
            }
        }

        lock.lock()
        captured.append(contentsOf: mono)
        tapCallbacks += 1
        lock.unlock()
    }

    // MARK: - Resampling

    /// Downsamples to 16 kHz.
    ///
    /// For the integer ratios real hardware produces (48k/3, 32k/2) this
    /// averages each group of input samples rather than picking one. The average
    /// is a crude low-pass, which matters: plain decimation folds everything
    /// above 8 kHz back into the speech band as aliasing, and sibilants are
    /// exactly what lands up there.
    static func resample(_ samples: [Float], from rate: Double) -> [Float] {
        guard rate > 0, !samples.isEmpty else { return [] }
        if rate == targetSampleRate { return samples }

        let ratio = rate / targetSampleRate
        let rounded = ratio.rounded()

        if abs(ratio - rounded) < 1e-9, rounded >= 2 {
            let factor = Int(rounded)
            let count = samples.count / factor
            guard count > 0 else { return [] }
            var output = [Float](repeating: 0, count: count)
            for i in 0..<count {
                var sum: Float = 0
                for j in 0..<factor { sum += samples[i * factor + j] }
                output[i] = sum / Float(factor)
            }
            return output
        }

        // Non-integer ratio (44.1 kHz hardware): linear interpolation.
        let count = Int(Double(samples.count) / ratio)
        guard count > 0 else { return [] }
        var output = [Float](repeating: 0, count: count)
        let last = samples.count - 1
        for i in 0..<count {
            let position = Double(i) * ratio
            let index = min(Int(position), last)
            let next = min(index + 1, last)
            let fraction = Float(position - Double(index))
            output[i] = samples[index] + (samples[next] - samples[index]) * fraction
        }
        return output
    }

    enum RecorderError: LocalizedError {
        case noInputDevice
        var errorDescription: String? { "No microphone input device is available." }
    }
}
