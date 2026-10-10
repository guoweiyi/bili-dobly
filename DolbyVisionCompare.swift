// Native, non-destructive Dolby Vision Profile 8.4 diagnostic.
// ON/OFF uses Apple's documented AVPlayerItem per-frame metadata switch.
// Both views retain the same file and native HLG/HDR rendering pipeline.
// https://developer.apple.com/av-foundation/Incorporating-HDR-video-with-Dolby-Vision-into-your-apps.pdf
// Build: ./scripts/build_apple_tools.sh
// The app opens a file picker when no file is supplied and starts paused/muted.
// Run its executable with --check FILE for a headless configuration check.

import Foundation
import AppKit
import AVKit
import AVFoundation
import CoreMedia
import UniformTypeIdentifiers

private func clockText(_ seconds: Double) -> String {
    guard seconds.isFinite, seconds >= 0 else { return "--:--.--" }
    let minutes = Int(seconds) / 60
    return String(format: "%02d:%05.2f", minutes, seconds - Double(minutes * 60))
}

private func dvConfiguration(_ descriptions: [CMFormatDescription]) -> [String: Any]? {
    for description in descriptions {
        guard let extensions = CMFormatDescriptionGetExtensions(description),
              let atoms = (extensions as NSDictionary)["SampleDescriptionExtensionAtoms"] as? NSDictionary else { continue }
        for name in ["dvvC", "dvcC"] {
            if let data = atoms[name] as? Data, data.count >= 5 {
                let bytes = Array(data)
                return ["atom": name, "profile": Int(bytes[2] >> 1),
                        "compatibilityID": Int(bytes[4] >> 4),
                        "rpuPresent": (bytes[3] & 4) != 0]
            }
        }
    }
    return nil
}

@MainActor
private final class ComparisonApp: NSObject, NSApplicationDelegate, NSWindowDelegate {
    private var window: NSWindow!
    private let onPlayer = AVPlayer()
    private let offPlayer = AVPlayer()
    private let onView = AVPlayerView()
    private let offView = AVPlayerView()
    private let status = NSTextField(wrappingLabelWithString: "打开本地 DV 8.4 MP4；两侧使用同一原文件，初始暂停、静音。")
    private let metrics = NSTextField(labelWithString: "00:00.00")
    private let scrubber = NSSlider(value: 0, minValue: 0, maxValue: 1, target: nil, action: nil)
    private var playButton: NSButton!
    private var soundButton: NSButton!
    private var timeObserver: Any?
    private var seekTask: Task<Void, Never>?
    private var loadTask: Task<Void, Never>?
    private var duration: Double = 0
    private var requestedPlayback = false
    private var seeking = false
    private var fileGeneration = 0
    private var seekGeneration = 0
    private var queuedFile: URL?
    private let initialFile: URL?
    private var activePicker: NSOpenPanel?

    init(initialFile: URL?) {
        self.initialFile = initialFile
        super.init()
        for player in [onPlayer, offPlayer] {
            player.isMuted = true
            // Apple's scheduled host-clock API requires this to be false.
            player.automaticallyWaitsToMinimizeStalling = false
            player.actionAtItemEnd = .pause
        }
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        buildMenu()
        buildWindow()
        timeObserver = onPlayer.addPeriodicTimeObserver(
            forInterval: CMTime(seconds: 0.25, preferredTimescale: 600), queue: .main
        ) { [weak self] _ in
            Task { @MainActor in self?.updateMetrics() }
        }
        if let queuedFile = queuedFile { load(queuedFile) }
        else if let initialFile = initialFile { load(initialFile) }
        else { openFile() }
    }

    func application(_ sender: NSApplication, openFile filename: String) -> Bool {
        let url = URL(fileURLWithPath: filename)
        if window == nil { queuedFile = url } else { load(url) }
        return true
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }

    func applicationWillTerminate(_ notification: Notification) {
        seekTask?.cancel()
        loadTask?.cancel()
        if let timeObserver = timeObserver { onPlayer.removeTimeObserver(timeObserver) }
        onPlayer.pause()
        offPlayer.pause()
        NotificationCenter.default.removeObserver(self)
    }

    func windowWillClose(_ notification: Notification) {
        pauseBoth()
        seekTask?.cancel()
        loadTask?.cancel()
    }

    private func buildMenu() {
        let menu = NSMenu()
        let root = NSMenuItem()
        let appMenu = NSMenu()
        let open = NSMenuItem(title: "打开视频…", action: #selector(openFile), keyEquivalent: "o")
        open.target = self
        appMenu.addItem(open)
        appMenu.addItem(.separator())
        appMenu.addItem(NSMenuItem(title: "退出 DV 元数据对照", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q"))
        root.submenu = appMenu
        menu.addItem(root)
        NSApplication.shared.mainMenu = menu
    }

    private func makeLabel(_ text: String, size: CGFloat, weight: NSFont.Weight) -> NSTextField {
        let label = NSTextField(wrappingLabelWithString: text)
        label.font = .systemFont(ofSize: size, weight: weight)
        return label
    }

    private func panel(_ view: AVPlayerView, title: String, subtitle: String) -> NSStackView {
        view.controlsStyle = .none
        view.videoGravity = .resizeAspect
        view.translatesAutoresizingMaskIntoConstraints = false
        let stack = NSStackView(views: [makeLabel(title, size: 18, weight: .semibold),
                                      makeLabel(subtitle, size: 12, weight: .regular), view])
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = 8
        view.widthAnchor.constraint(equalTo: stack.widthAnchor).isActive = true
        view.heightAnchor.constraint(equalTo: view.widthAnchor, multiplier: 9.0 / 16.0).isActive = true
        return stack
    }

    private func buildWindow() {
        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1280, height: 710),
                          styleMask: [.titled, .closable, .miniaturizable, .resizable],
                          backing: .buffered, defer: false)
        window.title = "DV 逐帧元数据对照 · 原文件不变"
        window.minSize = NSSize(width: 1000, height: 620)
        window.isReleasedWhenClosed = false
        window.delegate = self
        onView.player = onPlayer
        offView.player = offPlayer
        let left = panel(onView, title: "逐帧 HDR 元数据：开启", subtitle: "请求使用 DV 8.4 元数据 · appliesPerFrameHDRDisplayMetadata = true")
        let right = panel(offView, title: "逐帧 HDR 元数据：关闭", subtitle: "DV 8.4 / HLG：忽略逐帧元数据，保留 HLG 基础层与系统 HDR 管线")
        let views = NSStackView(views: [left, right])
        views.orientation = .horizontal
        views.distribution = .fillEqually
        views.alignment = .top
        views.spacing = 20
        let open = NSButton(title: "打开视频…", target: self, action: #selector(openFile))
        playButton = NSButton(title: "同步播放", target: self, action: #selector(togglePlay))
        playButton.isEnabled = false
        let restart = NSButton(title: "回到开头", target: self, action: #selector(restart))
        soundButton = NSButton(checkboxWithTitle: "播放开启侧的声音", target: self, action: #selector(toggleSound))
        soundButton.state = .off
        let controls = NSStackView(views: [open, playButton, restart, soundButton])
        controls.orientation = .horizontal
        controls.spacing = 12
        scrubber.target = self
        scrubber.action = #selector(scrub)
        scrubber.isContinuous = true
        scrubber.isEnabled = false
        metrics.font = .monospacedDigitSystemFont(ofSize: 12, weight: .regular)
        let note = makeLabel("两侧均由系统 AVPlayer/AVKit 渲染。关闭侧用于查看 DV 8.4 的 HLG 基础层；其他 DV profile 不一定具备 HLG 兼容层。整体更亮不代表更准确，差异不明显也不证明 DV 未工作。同步播放可显示漂移；暂停后拖动到同一时间点适合仔细对照。此工具不认证屏幕色彩准确性，也不改变视频、显示亮度或系统设置。", size: 12, weight: .regular)
        note.textColor = .secondaryLabelColor
        status.font = .systemFont(ofSize: 13)
        let content = NSStackView(views: [status, views, scrubber, metrics, controls, note])
        content.orientation = .vertical
        content.alignment = .leading
        content.spacing = 14
        content.translatesAutoresizingMaskIntoConstraints = false
        let host = window.contentView!
        host.addSubview(content)
        NSLayoutConstraint.activate([
            content.leadingAnchor.constraint(equalTo: host.leadingAnchor, constant: 22),
            content.trailingAnchor.constraint(equalTo: host.trailingAnchor, constant: -22),
            content.topAnchor.constraint(equalTo: host.topAnchor, constant: 20),
            content.bottomAnchor.constraint(lessThanOrEqualTo: host.bottomAnchor, constant: -20),
            views.widthAnchor.constraint(equalTo: content.widthAnchor),
            scrubber.widthAnchor.constraint(equalTo: content.widthAnchor),
            note.widthAnchor.constraint(equalTo: content.widthAnchor),
            status.widthAnchor.constraint(equalTo: content.widthAnchor)
        ])
        window.center()
        window.makeKeyAndOrderFront(nil)
        NSApplication.shared.activate(ignoringOtherApps: true)
    }

    @objc private func openFile() {
        guard activePicker == nil else { return }
        pauseBoth()
        seekTask?.cancel()
        onPlayer.currentItem?.cancelPendingSeeks()
        offPlayer.currentItem?.cancelPendingSeeks()
        seeking = false
        let picker = NSOpenPanel()
        activePicker = picker
        picker.title = "选择本地 HDR 视频"
        picker.prompt = "打开"
        picker.allowedContentTypes = [.movie, .mpeg4Movie, .quickTimeMovie]
        picker.allowsMultipleSelection = false
        picker.canChooseDirectories = false
        picker.canChooseFiles = true
        picker.beginSheetModal(for: window) { [weak self] response in
            self?.activePicker = nil
            if response == .OK, let url = picker.url { self?.load(url) }
        }
    }

    private func load(_ url: URL) {
        var isDirectory: ObjCBool = false
        guard url.isFileURL,
              FileManager.default.fileExists(atPath: url.path, isDirectory: &isDirectory),
              !isDirectory.boolValue,
              FileManager.default.isReadableFile(atPath: url.path) else {
            status.stringValue = "无法读取本地视频文件：\(url.lastPathComponent)；请重新选择。"
            return
        }
        pauseBoth()
        seekTask?.cancel()
        loadTask?.cancel()
        fileGeneration += 1
        let generation = fileGeneration
        duration = 0
        playButton.isEnabled = false
        scrubber.isEnabled = false
        soundButton.state = .off
        onPlayer.isMuted = true
        offPlayer.isMuted = true
        let onItem = AVPlayerItem(url: url)
        let offItem = AVPlayerItem(url: url)
        onItem.appliesPerFrameHDRDisplayMetadata = true
        offItem.appliesPerFrameHDRDisplayMetadata = false
        onPlayer.replaceCurrentItem(with: onItem)
        offPlayer.replaceCurrentItem(with: offItem)
        NotificationCenter.default.removeObserver(self, name: AVPlayerItem.didPlayToEndTimeNotification, object: nil)
        NotificationCenter.default.addObserver(self, selector: #selector(reachedEnd), name: AVPlayerItem.didPlayToEndTimeNotification, object: onItem)
        window.representedURL = url
        status.stringValue = "正在加载：\(url.lastPathComponent) · 默认静音"
        loadTask = Task { @MainActor [weak self] in
            guard let self = self else { return }
            do {
                let playable = try await onItem.asset.load(.isPlayable)
                let time = try await onItem.asset.load(.duration)
                let tracks = try await onItem.asset.loadTracks(withMediaType: .video)
                var config: [String: Any]?
                if let track = tracks.first { config = dvConfiguration(try await track.load(.formatDescriptions)) }
                guard !Task.isCancelled, self.fileGeneration == generation else { return }
                let seconds = CMTimeGetSeconds(time)
                guard playable, !tracks.isEmpty, seconds.isFinite, seconds > 0 else {
                    self.status.stringValue = "系统无法正常读取该文件；请先使用批量工具 verify。"
                    return
                }
                self.duration = seconds
                self.scrubber.maxValue = seconds
                self.scrubber.isEnabled = true
                self.playButton.isEnabled = true
                let profile = config?["profile"] as? Int
                let compatibility = config?["compatibilityID"] as? Int
                let text = profile == 8 && compatibility == 4 ? "DV 8.4 / HLG 兼容" :
                    (config == nil ? "未找到 DV 配置，差异可能为空" : "DV Profile \(profile ?? 0)，关闭侧不保证 HLG 兼容")
                self.status.stringValue = "\(url.lastPathComponent) · \(text) · 逐帧元数据 ON / OFF · 初始暂停、静音"
                self.seekBoth(to: 0, resume: false)
            } catch {
                guard self.fileGeneration == generation else { return }
                self.status.stringValue = "读取失败：\(error.localizedDescription)"
            }
        }
    }

    private func pauseBoth() {
        onPlayer.pause()
        offPlayer.pause()
        requestedPlayback = false
        playButton?.title = "同步播放"
    }

    private func startBoth(at time: CMTime) {
        let host = CMTimeAdd(CMClockGetTime(CMClockGetHostTimeClock()), CMTime(seconds: 0.2, preferredTimescale: 1_000_000_000))
        onPlayer.setRate(1, time: time, atHostTime: host)
        offPlayer.setRate(1, time: time, atHostTime: host)
        requestedPlayback = true
        playButton.title = "同步暂停"
    }

    private func seekBoth(to seconds: Double, resume: Bool) {
        guard duration > 0 else { return }
        pauseBoth()
        requestedPlayback = resume
        playButton.title = resume ? "同步暂停" : "同步播放"
        seekTask?.cancel()
        onPlayer.currentItem?.cancelPendingSeeks()
        offPlayer.currentItem?.cancelPendingSeeks()
        seekGeneration += 1
        let generation = seekGeneration
        let file = fileGeneration
        seeking = true
        let target = CMTime(seconds: max(0, min(seconds, duration)), preferredTimescale: 60_000)
        seekTask = Task { @MainActor [weak self] in
            guard let self = self else { return }
            let onReady = await self.onPlayer.seek(to: target, toleranceBefore: .zero, toleranceAfter: .zero)
            guard !Task.isCancelled, self.seekGeneration == generation, self.fileGeneration == file else { return }
            let offReady = await self.offPlayer.seek(to: target, toleranceBefore: .zero, toleranceAfter: .zero)
            guard !Task.isCancelled, self.seekGeneration == generation, self.fileGeneration == file else { return }
            self.seeking = false
            if onReady && offReady && resume { self.startBoth(at: target) }
            else if !onReady || !offReady { self.pauseBoth() }
            self.updateMetrics()
        }
    }

    @objc private func togglePlay() {
        if requestedPlayback {
            seekTask?.cancel()
            onPlayer.currentItem?.cancelPendingSeeks()
            offPlayer.currentItem?.cancelPendingSeeks()
            seeking = false
            pauseBoth()
            return
        }
        var seconds = CMTimeGetSeconds(onPlayer.currentTime())
        if !seconds.isFinite || seconds >= duration - 0.02 { seconds = 0 }
        seekBoth(to: seconds, resume: true)
    }

    @objc private func scrub() { seekBoth(to: scrubber.doubleValue, resume: requestedPlayback) }
    @objc private func restart() { seekBoth(to: 0, resume: requestedPlayback) }
    @objc private func reachedEnd() { pauseBoth(); updateMetrics() }
    @objc private func toggleSound() {
        onPlayer.isMuted = soundButton.state != .on
        offPlayer.isMuted = true
    }

    private func updateMetrics() {
        let on = CMTimeGetSeconds(onPlayer.currentTime())
        let off = CMTimeGetSeconds(offPlayer.currentTime())
        if !seeking && on.isFinite { scrubber.doubleValue = on }
        let delta = on.isFinite && off.isFinite ? abs(on - off) : 0
        let screen = window.screen
        let edr = screen.map { String(format: " · EDR %.2f / %.2f", $0.maximumExtendedDynamicRangeColorComponentValue,
                                       $0.maximumPotentialExtendedDynamicRangeColorComponentValue) } ?? ""
        metrics.stringValue = "ON \(clockText(on))  |  OFF \(clockText(off))  /  \(clockText(duration))  · 两侧时间差 \(String(format: "%.3f", delta)) s\(edr)"
    }
}

@main
private struct CompareMain {
    @MainActor static func main() async {
        let args = Array(CommandLine.arguments.dropFirst())
        if args.first == "--help" {
            print("DolbyVisionCompare [FILE]\nDolbyVisionCompare --check FILE\nNative AVPlayer metadata ON/OFF comparison. No FILE opens a picker. GUI starts paused and muted. --check never opens a window or plays audio.")
            return
        }
        if args.first == "--check" {
            do {
                guard args.count == 2 else { throw NSError(domain: "DVCompare", code: 64, userInfo: [NSLocalizedDescriptionKey: "--check requires one local file"] ) }
                let url = URL(fileURLWithPath: args[1]).standardizedFileURL
                var directory: ObjCBool = false
                guard FileManager.default.fileExists(atPath: url.path, isDirectory: &directory),
                      !directory.boolValue, FileManager.default.isReadableFile(atPath: url.path) else {
                    throw NSError(domain: "DVCompare", code: 66, userInfo: [NSLocalizedDescriptionKey: "Cannot read local video file: \(url.path)"])
                }
                let asset = AVURLAsset(url: url)
                let playable = try await asset.load(.isPlayable)
                let time = try await asset.load(.duration)
                let tracks = try await asset.loadTracks(withMediaType: .video)
                var config: [String: Any]?
                if let track = tracks.first { config = dvConfiguration(try await track.load(.formatDescriptions)) }
                let on = AVPlayerItem(asset: asset)
                let off = AVPlayerItem(asset: asset)
                on.appliesPerFrameHDRDisplayMetadata = true
                off.appliesPerFrameHDRDisplayMetadata = false
                let report: [String: Any] = ["path": url.path, "isPlayable": playable,
                    "durationSeconds": CMTimeGetSeconds(time).isFinite ? CMTimeGetSeconds(time) as Any : NSNull(),
                    "dolbyVisionConfiguration": config as Any? ?? NSNull(),
                    "metadataOnProperty": on.appliesPerFrameHDRDisplayMetadata,
                    "metadataOffProperty": off.appliesPerFrameHDRDisplayMetadata,
                    "guiInitialMuted": true, "guiInitialPaused": true,
                    "uiLaunched": false, "playbackPerformed": false,
                    "comparisonVisuallyVerified": false, "hdrEligible": AVPlayer.eligibleForHDRPlayback]
                let data = try JSONSerialization.data(withJSONObject: report, options: [.prettyPrinted, .sortedKeys])
                FileHandle.standardOutput.write(data)
                FileHandle.standardOutput.write(Data([10]))
            } catch {
                let data = try? JSONSerialization.data(withJSONObject: ["error": error.localizedDescription])
                if let data = data { FileHandle.standardOutput.write(data); FileHandle.standardOutput.write(Data([10])) }
                exit(1)
            }
            return
        }
        guard args.isEmpty || (args.count == 1 && !args[0].hasPrefix("-")) else {
            FileHandle.standardError.write(Data("Usage: DolbyVisionCompare [FILE] | --check FILE | --help\n".utf8))
            exit(64)
        }
        let app = NSApplication.shared
        app.setActivationPolicy(.regular)
        let delegate = ComparisonApp(initialFile: args.first.map { URL(fileURLWithPath: $0).standardizedFileURL })
        app.delegate = delegate
        withExtendedLifetime(delegate) { app.run() }
    }
}
