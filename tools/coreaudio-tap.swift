// coreaudio-tap: Core Audio Process Tap helper (macOS 14.4+).
//
//   coreaudio-tap --capture [--mic "<name>"] [--rate 16000]   mono s16le PCM -> stdout
//   coreaudio-tap --list                                      list input devices
//   coreaudio-tap --cleanup [Name]                            destroy stale aggregates
//
// Capture mixes mic + system-output tap through ONE aggregate device and ONE IOProc,
// so there is a single clock domain (no avfoundation reclock race).
import CoreAudio
import AudioToolbox
import Foundation

let aggName = "MeetingTap"

func fail(_ msg: String) -> Never {
    FileHandle.standardError.write(Data(("coreaudio-tap: " + msg + "\n").utf8))
    exit(1)
}

func prop<T>(_ obj: AudioObjectID, _ sel: AudioObjectPropertySelector,
             scope: AudioObjectPropertyScope = kAudioObjectPropertyScopeGlobal, _ def: T) -> T {
    var addr = AudioObjectPropertyAddress(mSelector: sel, mScope: scope, mElement: kAudioObjectPropertyElementMain)
    var v = def
    var size = UInt32(MemoryLayout<T>.size)
    let st = AudioObjectGetPropertyData(obj, &addr, 0, nil, &size, &v)
    if st != 0 { fail("property \(sel) failed: \(st)") }
    return v
}

func deviceList() -> [AudioObjectID] {
    var addr = AudioObjectPropertyAddress(mSelector: kAudioHardwarePropertyDevices,
        mScope: kAudioObjectPropertyScopeGlobal, mElement: kAudioObjectPropertyElementMain)
    var size: UInt32 = 0
    AudioObjectGetPropertyDataSize(AudioObjectID(kAudioObjectSystemObject), &addr, 0, nil, &size)
    var ids = [AudioObjectID](repeating: 0, count: Int(size) / MemoryLayout<AudioObjectID>.size)
    AudioObjectGetPropertyData(AudioObjectID(kAudioObjectSystemObject), &addr, 0, nil, &size, &ids)
    return ids
}

func str(_ obj: AudioObjectID, _ sel: AudioObjectPropertySelector) -> String {
    var addr = AudioObjectPropertyAddress(mSelector: sel, mScope: kAudioObjectPropertyScopeGlobal,
                                          mElement: kAudioObjectPropertyElementMain)
    var cf: Unmanaged<CFString>?
    var size = UInt32(MemoryLayout<Unmanaged<CFString>?>.size)
    guard AudioObjectGetPropertyData(obj, &addr, 0, nil, &size, &cf) == 0, let s = cf else { return "" }
    return s.takeRetainedValue() as String
}

func inputChannels(_ dev: AudioObjectID) -> Int {
    var addr = AudioObjectPropertyAddress(mSelector: kAudioDevicePropertyStreamConfiguration,
        mScope: kAudioDevicePropertyScopeInput, mElement: kAudioObjectPropertyElementMain)
    var size: UInt32 = 0
    guard AudioObjectGetPropertyDataSize(dev, &addr, 0, nil, &size) == 0, size > 0 else { return 0 }
    let raw = UnsafeMutableRawPointer.allocate(byteCount: Int(size), alignment: 16)
    defer { raw.deallocate() }
    guard AudioObjectGetPropertyData(dev, &addr, 0, nil, &size, raw) == 0 else { return 0 }
    let list = UnsafeMutableAudioBufferListPointer(raw.assumingMemoryBound(to: AudioBufferList.self))
    return list.reduce(0) { $0 + Int($1.mNumberChannels) }
}

func findInput(matching name: String?) -> AudioObjectID? {
    let inputs = deviceList().filter { inputChannels($0) > 0 && !str($0, kAudioObjectPropertyName).hasPrefix(aggName) }
    if let n = name, !n.isEmpty {
        return inputs.first { str($0, kAudioObjectPropertyName).localizedCaseInsensitiveContains(n) }
    }
    var addr = AudioObjectPropertyAddress(mSelector: kAudioHardwarePropertyDefaultInputDevice,
        mScope: kAudioObjectPropertyScopeGlobal, mElement: kAudioObjectPropertyElementMain)
    var d = AudioObjectID(0); var size = UInt32(4)
    AudioObjectGetPropertyData(AudioObjectID(kAudioObjectSystemObject), &addr, 0, nil, &size, &d)
    return d == 0 ? nil : d
}

func cleanup(_ name: String) {
    for d in deviceList() where str(d, kAudioObjectPropertyName).hasPrefix(name) {
        let st = AudioHardwareDestroyAggregateDevice(d)
        FileHandle.standardError.write(Data("destroyed \(name) device \(d): \(st)\n".utf8))
    }
}

// ---- args ----
var args = Array(CommandLine.arguments.dropFirst())
func opt(_ k: String) -> String? {
    if let i = args.firstIndex(of: k), i + 1 < args.count { return args[i + 1] }
    return nil
}

if args.contains("--list") {
    for d in deviceList() where inputChannels(d) > 0 {
        print("\(d)\t\(str(d, kAudioObjectPropertyName))\t\(inputChannels(d))ch")
    }
    exit(0)
}
if args.contains("--cleanup") {
    cleanup(args.drop(while: { $0 != "--cleanup" }).dropFirst().first ?? aggName)
    exit(0)
}
guard args.contains("--capture") else { fail("usage: --capture [--mic name] [--rate 16000] | --list | --cleanup") }

let outRate = Double(opt("--rate") ?? "16000") ?? 16000
cleanup(aggName)  // stale aggregates from a crashed run

// Tap: all system output, mixed to stereo, not muting the speakers.
let tapDesc = CATapDescription(stereoGlobalTapButExcludeProcesses: [])
tapDesc.name = aggName
tapDesc.muteBehavior = .unmuted
var tapID = AudioObjectID(kAudioObjectUnknown)
var st = AudioHardwareCreateProcessTap(tapDesc, &tapID)
if st != 0 { fail("AudioHardwareCreateProcessTap failed (\(st)); grant Audio Recording permission, macOS 14.4+") }

guard let mic = findInput(matching: opt("--mic")) else { fail("no mic input device found (try --list)") }
let micUID = str(mic, kAudioDevicePropertyDeviceUID)

let desc: [String: Any] = [
    kAudioAggregateDeviceNameKey: aggName,
    kAudioAggregateDeviceUIDKey: "com.meeting-agent.\(UUID().uuidString)",
    kAudioAggregateDeviceMainSubDeviceKey: micUID,
    kAudioAggregateDeviceIsPrivateKey: true,
    kAudioAggregateDeviceIsStackedKey: false,
    kAudioAggregateDeviceSubDeviceListKey: [[kAudioSubDeviceUIDKey: micUID]],
    kAudioAggregateDeviceTapListKey: [[
        kAudioSubTapUIDKey: tapDesc.uuid.uuidString,
        kAudioSubTapDriftCompensationKey: true,
    ]],
]
var aggID = AudioObjectID(kAudioObjectUnknown)
st = AudioHardwareCreateAggregateDevice(desc as CFDictionary, &aggID)
if st != 0 { fail("create aggregate failed: \(st)") }

func teardown() {
    AudioHardwareDestroyAggregateDevice(aggID)
    AudioHardwareDestroyProcessTap(tapID)
}
var sigSources: [DispatchSourceSignal] = []
for sig in [SIGINT, SIGTERM, SIGPIPE] {
    signal(sig, SIG_IGN)
    let s = DispatchSource.makeSignalSource(signal: sig, queue: .global())
    s.setEventHandler { teardown(); exit(0) }
    s.resume()
    sigSources.append(s)
}

// Input layout of the aggregate: first the mic stream(s), then the tap stream (stereo).
var fmtAddr = AudioObjectPropertyAddress(mSelector: kAudioDevicePropertyNominalSampleRate,
    mScope: kAudioObjectPropertyScopeGlobal, mElement: kAudioObjectPropertyElementMain)
var devRate = Float64(0); var rsz = UInt32(8)
AudioObjectGetPropertyData(aggID, &fmtAddr, 0, nil, &rsz, &devRate)
if devRate <= 0 { devRate = 48000 }
let micCh = max(1, inputChannels(mic))
FileHandle.standardError.write(Data("aggregate \(aggID) rate=\(devRate) micCh=\(micCh) -> \(outRate) Hz mono\n".utf8))

// Mono mix + linear-interp resample to outRate, written as s16le.
let ratio = devRate / outRate
var phase = 0.0
var last: Float = 0
let outFD = FileHandle.standardOutput.fileDescriptor

var procID: AudioDeviceIOProcID?
st = AudioDeviceCreateIOProcIDWithBlock(&procID, aggID, nil) { _, inData, _, _, _ in
    let list = UnsafeMutableAudioBufferListPointer(UnsafeMutablePointer(mutating: inData))
    guard list.count > 0 else { return }
    // Mono-mix every input stream (mic + tap) frame by frame.
    let frames = Int(list[0].mDataByteSize) / (MemoryLayout<Float>.size * max(1, Int(list[0].mNumberChannels)))
    var mono = [Float](repeating: 0, count: frames)
    var streams = 0
    for b in list {
        guard let p = b.mData?.assumingMemoryBound(to: Float.self) else { continue }
        let ch = max(1, Int(b.mNumberChannels))
        let n = min(frames, Int(b.mDataByteSize) / (MemoryLayout<Float>.size * ch))
        for f in 0..<n {
            var s: Float = 0
            for c in 0..<ch { s += p[f * ch + c] }
            mono[f] += s / Float(ch)
        }
        streams += 1
    }
    let gain = 1 / Float(max(1, streams))
    var out = [Int16](); out.reserveCapacity(Int(Double(frames) / ratio) + 2)
    while phase < Double(frames) {
        let i = Int(phase); let frac = Float(phase - Double(i))
        let a = i == 0 ? last : mono[i - 1], b = mono[i]
        let v = max(-1, min(1, (a + (b - a) * frac) * gain * 1.5))
        out.append(Int16(v * 32767))
        phase += ratio
    }
    phase -= Double(frames)
    last = mono[frames - 1]
    out.withUnsafeBytes { raw in
        var off = 0
        while off < raw.count {
            let w = write(outFD, raw.baseAddress! + off, raw.count - off)
            if w <= 0 { teardown(); exit(0) }
            off += w
        }
    }
}
if st != 0 { teardown(); fail("create IOProc failed: \(st)") }
st = AudioDeviceStart(aggID, procID)
if st != 0 { teardown(); fail("start failed: \(st)") }
FileHandle.standardError.write(Data("capturing\n".utf8))
dispatchMain()
