import CoreAudio
import Foundation

/// Silences the default output device while dictating, and puts it back.
///
/// Two reasons it helps: a call or video playing through speakers bleeds into
/// the microphone and contaminates the transcript, and you rarely want to talk
/// over whatever is playing anyway.
///
/// It is off by default. Muting the machine is the kind of side effect that
/// should be asked for, not assumed -- sometimes you are dictating a note while
/// deliberately listening to something.
enum SystemAudio {

    /// What we changed, so it can be put back exactly. Nil means we changed
    /// nothing and must not touch anything on restore.
    private struct Restore {
        let device: AudioDeviceID
        let wasMuted: UInt32?
        let channelVolumes: [(UInt32, Float32)]
    }

    private static var restore: Restore?

    // MARK: - Public

    static func muteIfPossible() {
        guard restore == nil, let device = defaultOutputDevice() else { return }

        // Preferred: the device's own mute switch. It restores exactly and does
        // not fight whatever volume the user had set.
        if let current = readMute(device) {
            if current == 1 {
                // Already muted by the user. Record that we did nothing, so
                // restore does not helpfully unmute something they muted.
                restore = Restore(device: device, wasMuted: nil, channelVolumes: [])
                Log.write("audio: already muted, leaving it alone")
                return
            }
            if writeMute(device, 1) {
                restore = Restore(device: device, wasMuted: current, channelVolumes: [])
                Log.write("audio: muted output device")
                return
            }
        }

        // Fallback for devices with no mute control (common on HDMI and some
        // external DACs): drop the per-channel volumes and restore them after.
        var saved: [(UInt32, Float32)] = []
        for channel in UInt32(1)...UInt32(2) {
            if let volume = readVolume(device, channel: channel) {
                if writeVolume(device, channel: channel, value: 0) {
                    saved.append((channel, volume))
                }
            }
        }
        if !saved.isEmpty {
            restore = Restore(device: device, wasMuted: nil, channelVolumes: saved)
            Log.write("audio: zeroed \(saved.count) channel volume(s)")
        } else {
            Log.write("audio: output device supports neither mute nor volume control")
        }
    }

    static func restoreIfMuted() {
        guard let state = restore else { return }
        restore = nil

        if let previous = state.wasMuted {
            _ = writeMute(state.device, previous)
            Log.write("audio: unmuted output device")
        }
        for (channel, volume) in state.channelVolumes {
            _ = writeVolume(state.device, channel: channel, value: volume)
        }
        if !state.channelVolumes.isEmpty {
            Log.write("audio: restored \(state.channelVolumes.count) channel volume(s)")
        }
    }

    // MARK: - CoreAudio plumbing

    private static func defaultOutputDevice() -> AudioDeviceID? {
        var address = AudioObjectPropertyAddress(
            mSelector: kAudioHardwarePropertyDefaultOutputDevice,
            mScope: kAudioObjectPropertyScopeGlobal,
            mElement: kAudioObjectPropertyElementMain)
        var device = AudioDeviceID(0)
        var size = UInt32(MemoryLayout<AudioDeviceID>.size)
        let status = AudioObjectGetPropertyData(
            AudioObjectID(kAudioObjectSystemObject), &address, 0, nil, &size, &device)
        guard status == noErr, device != kAudioObjectUnknown else { return nil }
        return device
    }

    private static func muteAddress() -> AudioObjectPropertyAddress {
        AudioObjectPropertyAddress(
            mSelector: kAudioDevicePropertyMute,
            mScope: kAudioDevicePropertyScopeOutput,
            mElement: kAudioObjectPropertyElementMain)
    }

    private static func readMute(_ device: AudioDeviceID) -> UInt32? {
        var address = muteAddress()
        guard AudioObjectHasProperty(device, &address) else { return nil }
        var value: UInt32 = 0
        var size = UInt32(MemoryLayout<UInt32>.size)
        guard AudioObjectGetPropertyData(device, &address, 0, nil, &size, &value) == noErr
        else { return nil }
        return value
    }

    private static func writeMute(_ device: AudioDeviceID, _ value: UInt32) -> Bool {
        var address = muteAddress()
        var settable: DarwinBoolean = false
        guard AudioObjectIsPropertySettable(device, &address, &settable) == noErr,
              settable.boolValue else { return false }
        var value = value
        return AudioObjectSetPropertyData(
            device, &address, 0, nil, UInt32(MemoryLayout<UInt32>.size), &value) == noErr
    }

    private static func volumeAddress(channel: UInt32) -> AudioObjectPropertyAddress {
        AudioObjectPropertyAddress(
            mSelector: kAudioDevicePropertyVolumeScalar,
            mScope: kAudioDevicePropertyScopeOutput,
            mElement: channel)
    }

    private static func readVolume(_ device: AudioDeviceID, channel: UInt32) -> Float32? {
        var address = volumeAddress(channel: channel)
        guard AudioObjectHasProperty(device, &address) else { return nil }
        var value: Float32 = 0
        var size = UInt32(MemoryLayout<Float32>.size)
        guard AudioObjectGetPropertyData(device, &address, 0, nil, &size, &value) == noErr
        else { return nil }
        return value
    }

    private static func writeVolume(_ device: AudioDeviceID, channel: UInt32, value: Float32) -> Bool {
        var address = volumeAddress(channel: channel)
        var settable: DarwinBoolean = false
        guard AudioObjectIsPropertySettable(device, &address, &settable) == noErr,
              settable.boolValue else { return false }
        var value = value
        return AudioObjectSetPropertyData(
            device, &address, 0, nil, UInt32(MemoryLayout<Float32>.size), &value) == noErr
    }
}
