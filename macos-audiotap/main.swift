// Транскрибатор Звук: запись системного звука macOS (Core Audio Process Tap, macOS 14.2+).
//
// Записывает то, что играет компьютер (все приложения), а не микрофон. Запускается сервером приложения:
//   open -n -a TranskribatorAudio.app --args <папка сессии> <pid сервера>
// Отдельное приложение нужно, чтобы macOS запрашивала разрешение «Запись системного звука» от его имени,
// как бы ни был запущен сервер (из Программ, Терминала или виджета).
//
// Обмен через файлы в папке сессии:
//   control      — команда от сервера: record | pause | stop
//   status.json  — состояние для сервера: state, seconds, level_db, error, rate, channels, pid
//   audio.wav    — запись (PCM 16 бит, частота и каналы устройства вывода)

import AVFoundation
import CoreAudio
import Foundation

let args = CommandLine.arguments
guard args.count >= 2 else {
    FileHandle.standardError.write("usage: audiotap <session-dir> [server-pid]\n".data(using: .utf8)!)
    exit(2)
}
let sessionDir = URL(fileURLWithPath: args[1], isDirectory: true)
let serverPid: pid_t? = args.count >= 3 ? pid_t(args[2]) : nil
let controlURL = sessionDir.appendingPathComponent("control")
let statusURL = sessionDir.appendingPathComponent("status.json")
let audioURL = sessionDir.appendingPathComponent("audio.wav")

// ───────── состояние ─────────
final class State: @unchecked Sendable {
    let lock = NSLock()
    var state = "starting"
    var framesWritten: Int64 = 0
    var levelDB: Double = -120
    var error: String?
    var rate: Double = 0
    var channels: UInt32 = 0
    var recording = false

    func with<T>(_ body: (State) -> T) -> T { lock.lock(); defer { lock.unlock() }; return body(self) }
}
let st = State()

func writeStatus() {
    let dict: [String: Any] = st.with { s in [
        "state": s.state,
        "seconds": s.rate > 0 ? Double(s.framesWritten) / s.rate : 0,
        "level_db": s.levelDB,
        "error": s.error as Any,
        "rate": s.rate,
        "channels": s.channels,
        "pid": Int(getpid()),
    ] }
    guard let data = try? JSONSerialization.data(withJSONObject: dict) else { return }
    let tmp = sessionDir.appendingPathComponent("status.json.tmp")
    try? data.write(to: tmp)
    _ = try? FileManager.default.replaceItemAt(statusURL, withItemAt: tmp)
}

func fail(_ message: String) -> Never {
    st.with { $0.state = "error"; $0.error = message }
    writeStatus()
    exit(1)
}

func check(_ status: OSStatus, _ what: String) {
    if status != noErr { fail("\(what): ошибка Core Audio \(status)") }
}

// ───────── Core Audio: отвод звука всех процессов + агрегатное устройство ─────────
func getProperty<T>(_ object: AudioObjectID, _ selector: AudioObjectPropertySelector, _ value: inout T) -> OSStatus {
    var address = AudioObjectPropertyAddress(mSelector: selector, mScope: kAudioObjectPropertyScopeGlobal,
                                             mElement: kAudioObjectPropertyElementMain)
    var size = UInt32(MemoryLayout<T>.size)
    return withUnsafeMutablePointer(to: &value) { AudioObjectGetPropertyData(object, &address, 0, nil, &size, $0) }
}

var outputDevice = AudioObjectID(kAudioObjectUnknown)
check(getProperty(AudioObjectID(kAudioObjectSystemObject), kAudioHardwarePropertyDefaultSystemOutputDevice, &outputDevice),
      "Не найдено устройство вывода звука")
func deviceUID(_ device: AudioObjectID) -> String? {
    var address = AudioObjectPropertyAddress(mSelector: kAudioDevicePropertyDeviceUID, mScope: kAudioObjectPropertyScopeGlobal,
                                             mElement: kAudioObjectPropertyElementMain)
    var size = UInt32(MemoryLayout<CFString?>.size)
    var uid: Unmanaged<CFString>?
    let status = withUnsafeMutablePointer(to: &uid) { AudioObjectGetPropertyData(device, &address, 0, nil, &size, $0) }
    return status == noErr ? uid?.takeRetainedValue() as String? : nil
}
guard let outputUID = deviceUID(outputDevice) else { fail("Не удалось получить UID устройства вывода") }

// все процессы, кроме себя; звук у слушателя не глушится
let tapDescription = CATapDescription(stereoGlobalTapButExcludeProcesses: [])
tapDescription.uuid = UUID()
tapDescription.muteBehavior = .unmuted
tapDescription.isPrivate = true
tapDescription.name = "Transkribator system audio"

var tapID = AudioObjectID(kAudioObjectUnknown)
check(AudioHardwareCreateProcessTap(tapDescription, &tapID), "Не удалось создать отвод системного звука")

var tapFormat = AudioStreamBasicDescription()
check(getProperty(tapID, kAudioTapPropertyFormat, &tapFormat), "Не удалось узнать формат звука")
guard let inputFormat = AVAudioFormat(streamDescription: &tapFormat) else { fail("Неподдерживаемый формат звука") }

let aggregateDescription: [String: Any] = [
    kAudioAggregateDeviceNameKey: "Transkribator Tap",
    kAudioAggregateDeviceUIDKey: UUID().uuidString,
    kAudioAggregateDeviceMainSubDeviceKey: outputUID,
    kAudioAggregateDeviceIsPrivateKey: true,
    kAudioAggregateDeviceIsStackedKey: false,
    kAudioAggregateDeviceTapAutoStartKey: true,
    kAudioAggregateDeviceSubDeviceListKey: [[kAudioSubDeviceUIDKey: outputUID]],
    kAudioAggregateDeviceTapListKey: [[kAudioSubTapDriftCompensationKey: true,
                                       kAudioSubTapUIDKey: tapDescription.uuid.uuidString]],
]
var aggregateID = AudioObjectID(kAudioObjectUnknown)
check(AudioHardwareCreateAggregateDevice(aggregateDescription as CFDictionary, &aggregateID),
      "Не удалось создать агрегатное устройство")

// ───────── файл: PCM 16 бит ─────────
let fileSettings: [String: Any] = [
    AVFormatIDKey: kAudioFormatLinearPCM,
    AVSampleRateKey: inputFormat.sampleRate,
    AVNumberOfChannelsKey: inputFormat.channelCount,
    AVLinearPCMBitDepthKey: 16,
    AVLinearPCMIsFloatKey: false,
    AVLinearPCMIsBigEndianKey: false,
]
let audioFile: AVAudioFile
do {
    audioFile = try AVAudioFile(forWriting: audioURL, settings: fileSettings,
                                commonFormat: inputFormat.commonFormat, interleaved: inputFormat.isInterleaved)
} catch {
    fail("Не удалось создать файл записи: \(error.localizedDescription)")
}
st.with { $0.rate = inputFormat.sampleRate; $0.channels = inputFormat.channelCount }

// запись на диск — в отдельной очереди, не в потоке реального времени
let writerQueue = DispatchQueue(label: "writer")
let ioQueue = DispatchQueue(label: "io", qos: .userInteractive)

var ioProcID: AudioDeviceIOProcID?
check(AudioDeviceCreateIOProcIDWithBlock(&ioProcID, aggregateID, ioQueue) { _, inInputData, _, _, _ in
    guard let source = AVAudioPCMBuffer(pcmFormat: inputFormat, bufferListNoCopy: inInputData, deallocator: nil),
          source.frameLength > 0 else { return }
    // уровень звука (RMS первого канала) — для индикатора в интерфейсе
    var sum: Float = 0
    let n = Int(source.frameLength)
    if let ch = source.floatChannelData?[0] {
        let stride = inputFormat.isInterleaved ? Int(inputFormat.channelCount) : 1
        for i in 0..<n { let v = ch[i * stride]; sum += v * v }
    }
    let rms = sqrt(sum / Float(max(n, 1)))
    let db = rms > 0 ? 20 * log10(Double(rms)) : -120
    let recording = st.with { s -> Bool in s.levelDB = max(db, -120); return s.recording }
    guard recording, let copy = AVAudioPCMBuffer(pcmFormat: inputFormat, frameCapacity: source.frameLength) else { return }
    copy.frameLength = source.frameLength
    let src = UnsafeMutableAudioBufferListPointer(UnsafeMutablePointer(mutating: source.audioBufferList))
    let dst = UnsafeMutableAudioBufferListPointer(copy.mutableAudioBufferList)
    for (s, d) in zip(src, dst) {
        if let sd = s.mData, let dd = d.mData { memcpy(dd, sd, Int(min(s.mDataByteSize, d.mDataByteSize))) }
    }
    writerQueue.async {
        do {
            try audioFile.write(from: copy)
            st.with { $0.framesWritten += Int64(copy.frameLength) }
        } catch {
            st.with { $0.error = "Ошибка записи файла: \(error.localizedDescription)" }
        }
    }
}, "Не удалось подключиться к звуку")

check(AudioDeviceStart(aggregateID, ioProcID), "Не удалось начать запись звука")

// ───────── управление ─────────
func finish() -> Never {
    AudioDeviceStop(aggregateID, ioProcID)
    if let ioProcID { AudioDeviceDestroyIOProcID(aggregateID, ioProcID) }
    AudioHardwareDestroyAggregateDevice(aggregateID)
    AudioHardwareDestroyProcessTap(tapID)
    writerQueue.sync {}           // дописать очередь
    st.with { $0.state = "stopped"; $0.recording = false }
    writeStatus()
    exit(0)
}

signal(SIGTERM, SIG_IGN)
let sigterm = DispatchSource.makeSignalSource(signal: SIGTERM, queue: .main)
sigterm.setEventHandler { finish() }
sigterm.resume()

var lastCommand = ""
let timer = DispatchSource.makeTimerSource(queue: .main)
var ticks = 0
timer.schedule(deadline: .now(), repeating: .milliseconds(50))
timer.setEventHandler {
    let command = (try? String(contentsOf: controlURL, encoding: .utf8))?.trimmingCharacters(in: .whitespacesAndNewlines) ?? "record"
    if command != lastCommand {
        lastCommand = command
        switch command {
        case "pause": st.with { $0.recording = false; $0.state = "paused" }
        case "stop": finish()
        default: st.with { $0.recording = true; $0.state = "recording" }
        }
    }
    // сервер пропал (упал или закрыт) — сохраняем записанное и выходим
    if let serverPid, kill(serverPid, 0) != 0, errno == ESRCH { finish() }
    ticks += 1
    if ticks % 4 == 0 { writeStatus() }
}
timer.resume()
writeStatus()
dispatchMain()
