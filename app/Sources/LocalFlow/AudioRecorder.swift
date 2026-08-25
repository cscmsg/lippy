import AVFoundation
import Foundation

/// Captures microphone audio and emits it as 16 kHz mono Float32 --
/// the format both Parakeet and Whisper want.
///
/// Audio is emitted in chunks *while* you speak, so the daemon can run a live
/// preview. The tap itself runs at the hardware's native format and does
/// nothing but copy floats; resampling happens off the realtime thread, in
/// blocks, as they fill.
///
/// An earlier design converted every tap buffer with an AVAudioConverter and
/// captured *nothing*: a 48 kHz -> 16 kHz resampler needs several buffers
/// before it can emit anything, and an input block that supplies one buffer and
/// then reports `.noDataNow` makes it return zero frames forever. It failed
/// silently -- engine running, tap firing, every converted buffer empty.
final class AudioRecorder {

    static let targetSampleRate: Double = 16_000

    /// Half a second of 16 kHz audio per chunk. Measured: the streaming decoder
    /// takes ~220ms to ingest 1s of audio, so per-chunk overhead dominates if
    /// you send every ~85ms tap buffer, and the preview falls behind realtime.
    /// Half a second keeps it comfortably ahead and still updates twice a second.
    private static let chunkFrames = 8_000

    /// Called off the realtime thread with each 16 kHz chunk as it fills.
    var onChunk: (([Float]) -> Void)?

    private let engine = AVAudioEngine()
    private let lock = NSLock()
    private var pendingNative: [Float] = []
    private var nativeSampleRate: Double = 0
    private var tapCallbacks = 0
    private var emittedFrames = 0
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

        lock.lock()
        pendingNative.removeAll()
        tapCallbacks = 0
        emittedFrames = 0
        lock.unlock()

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

    /// Stops capture and returns the tail -- whatever had not yet filled a chunk.
    @discardableResult
    func stop() -> [Float] {
        guard isRunning else { return [] }
        engine.inputNode.removeTap(onBus: 0)
        engine.stop()
        isRunning = false

        lock.lock()
        let remainder = pendingNative
        pendingNative.removeAll()
        let callbacks = tapCallbacks
        let emitted = emittedFrames
        lock.unlock()

        let tail = Self.resample(remainder, from: nativeSampleRate)
        Log.write("captured \(callbacks) buffers @\(Int(nativeSampleRate))Hz -> "
                  + "\(emitted) streamed + \(tail.count) tail = \(emitted + tail.count) @16kHz")
        return tail
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

        var ready: [[Float]] = []
        let blockSize = Self.nativeBlockSize(for: nativeSampleRate)

        lock.lock()
        pendingNative.append(contentsOf: mono)
        tapCallbacks += 1
        while pendingNative.count >= blockSize {
            let block = Array(pendingNative[0..<blockSize])
            pendingNative.removeFirst(blockSize)
            let chunk = Self.resample(block, from: nativeSampleRate)
            emittedFrames += chunk.count
            ready.append(chunk)
        }
        lock.unlock()

        // Emit outside the lock: the callback writes to a socket.
        for chunk in ready { onChunk?(chunk) }
    }

    /// How many native samples make one 16 kHz chunk.
    private static func nativeBlockSize(for rate: Double) -> Int {
        guard rate > 0 else { return chunkFrames }
        return max(1, Int((rate / targetSampleRate).rounded() * Double(chunkFrames)))
    }

    // MARK: - Resampling

    /// Downsamples to 16 kHz.
    ///
    /// For the integer ratios real hardware produces (48k/3, 32k/2) this
    /// averages each group of input samples rather than picking one. The average
    /// is a crude low-pass, which matters: plain decimation folds everything
    /// above 8 kHz back into the speech band as aliasing, and sibilants are
    /// exactly what lands up there.
    ///
    /// Chunk boundaries are exact for integer ratios. At 44.1 kHz each block is
    /// interpolated independently, which leaves a sub-sample seam per chunk --
    /// inaudible, and it reaches the model as a rounding difference.
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
