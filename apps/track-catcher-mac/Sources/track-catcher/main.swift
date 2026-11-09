// track-catcher: the CLI behind every button.
//
//   track-catcher analyze FILE            whole-track key, BPM and key changes
//   track-catcher wheel KEY               the ASCII Camelot wheel (KEY like 12B)
//   track-catcher inbox                   print the Inbox file's entries
//   track-catcher catch [--playlist NAME]        ask the running app to Catch
//   track-catcher now-playing [--playlist NAME]  ask the running app for Now playing
//   track-catcher listen | stop | tap | copy | settings | setup | quit
//   track-catcher compact | pin | half | double | copy-notes | copy-wheel
//   track-catcher bars | ring                    switch the bars or ring view
//   track-catcher copy-key --key 9A               copy one wheel key
//
// Commands for the running app go over a macOS distributed notification.
import Foundation
import TrackCatcherCore

#if canImport(AppKit)
import AppKit
#endif

func fail(_ msg: String, code: Int32 = 2) -> Never {
    FileHandle.standardError.write(Data("track-catcher: \(msg)\n".utf8))
    exit(code)
}

var args = Array(CommandLine.arguments.dropFirst())
guard let cmd = args.first else {
    fail("usage: track-catcher analyze FILE | wheel KEY | inbox | catch|now-playing [--playlist NAME] | listen|stop|tap|copy|settings|setup|quit|compact|pin|half|double|copy-notes|copy-wheel|bars|ring | copy-key --key KEY")
}
args.removeFirst()

func option(_ name: String) -> String? {
    guard let i = args.firstIndex(of: name) else { return nil }
    guard i + 1 < args.count else { fail("\(name) needs a value") }
    return args[i + 1]
}

switch cmd {
case "analyze":
    guard let path = args.first else { fail("analyze needs a file") }
    do {
        let exe = try EngineLocator.find()
        let summary = try FileAnalysis.run(executable: exe, file: URL(fileURLWithPath: path), duration: nil) { _ in }
        print(CopyText.trackResult(summary))
    } catch {
        fail("\(error)", code: 1)
    }
case "wheel":
    guard let code = args.first, let key = MusicalKey(camelot: code) else { fail("wheel needs a Camelot key like 12B") }
    print(CopyText.asciiWheel(current: key))
case "inbox":
    do {
        for e in try Inbox.standard().entries() {
            print("\(e.caughtAt)  \(e.artist) · \(e.title)  \(e.resultLine)")
        }
    } catch { fail("\(error)", code: 1) }
default:
    guard let command = AppCommand(rawValue: cmd) else { fail("unknown command \(cmd)") }
    // copy-key with no key would print "sent" while the app copies nothing.
    if command == .copyKey, option("--key") == nil { fail("copy-key needs --key with a Camelot key like 9A") }
    #if canImport(AppKit)
    var info: [String: String] = ["command": command.rawValue]
    if let pl = option("--playlist") { info["playlist"] = pl }
    if let k = option("--key") {
        guard MusicalKey(camelot: k) != nil else { fail("--key needs a Camelot key like 9A") }
        info["key"] = k
    }
    let running = NSRunningApplication.runningApplications(withBundleIdentifier: "com.opendj.trackcatcher")
    guard !running.isEmpty else { fail("Open DJ Track Catcher is not running", code: 1) }
    DistributedNotificationCenter.default().postNotificationName(
        NSNotification.Name(AppCommand.notificationName), object: nil, userInfo: info, deliverImmediately: true)
    print("sent \(command.rawValue)\(info["playlist"].map { " to \($0)" } ?? "")")
    #else
    fail("\(command.rawValue) talks to the running Mac app; this build has no AppKit", code: 1)
    #endif
}
