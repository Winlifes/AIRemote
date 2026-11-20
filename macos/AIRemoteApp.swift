import SwiftUI
import AppKit
import Darwin

struct ClientStatus: Decodable, Identifiable {
    let id: String
    let name: String
    let connected: Bool
    let statusLabel: String
}

struct RuntimeStatus: Decodable {
    var running: [String: Int] = [:]
    var connectorHealthy = false
    var agentRunning = false
    var relay = ""
    var providers: [ClientStatus] = []
    var logsDirectory: String?
    var ready: Bool { connectorHealthy && agentRunning }
}

struct UpdateStatus: Decodable {
    var latestVersion: String
    var available: Bool
    var plan: String?
}

struct GoalChannelStatus: Decodable {
    var configured = false
    var connected = false
    var message: String?
    init() {}
    enum CodingKeys: String, CodingKey { case configured, connected, message }
    init(from decoder: Decoder) throws {
        let values = try decoder.container(keyedBy: CodingKeys.self)
        configured = try values.decodeIfPresent(Bool.self, forKey: .configured) ?? false
        connected = try values.decodeIfPresent(Bool.self, forKey: .connected) ?? false
        message = try values.decodeIfPresent(String.self, forKey: .message)
    }
}

@MainActor final class ServiceModel: ObservableObject {
    @Published var status = RuntimeStatus()
    @Published var busy = false
    @Published var message = "正在检查服务…"
    @Published var error: String?
    @Published var update: UpdateStatus?
    @Published var announceUpdate = false
    @Published var updating = false
    @Published var updateMessage = ""
    @Published var updateProgress: UpdateProgress?
    @Published var goalChannel = GoalChannelStatus()
    @Published var goalMessage = ""
    @Published var autoCheck = UserDefaults.standard.object(forKey: "AutoCheckUpdates") as? Bool ?? true {
        didSet { UserDefaults.standard.set(autoCheck, forKey: "AutoCheckUpdates") }
    }
    private var checking = false
    private var launched = false
    private var configurationBlocked = false
    private var refreshTimer: Timer?

    deinit { refreshTimer?.invalidate() }

    var project: URL {
        let saved = UserDefaults.standard.string(forKey: "ProjectDirectory")
        let bundled = Bundle.main.object(forInfoDictionaryKey: "AIRemoteProjectDirectory") as? String
        let fallback = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Library/Application Support/AIRemote").path
        return URL(fileURLWithPath: saved ?? bundled ?? fallback, isDirectory: true)
    }
    var config: URL { project.appendingPathComponent(".local/mac-runtime.json") }
    var version: String { Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? "0.0.0" }
    var runtime: URL { Bundle.main.resourceURL!.appendingPathComponent("runtime", isDirectory: true) }

    func launch() {
        guard !launched else { return }
        launched = true
        if autoCheck { checkUpdates(manual: false, announce: true) }
        run(action: nil)
        let timer = Timer(timeInterval: 5, repeats: true) { [weak self] _ in
            Task { @MainActor in self?.refresh() }
        }
        RunLoop.main.add(timer, forMode: .common)
        refreshTimer = timer
    }

    func refresh() {
        checkIfDue()
        guard !busy && !checking && !configurationBlocked else { return }
        checking = true
        let directory = project
        Task {
            defer { checking = false }
            let result = await Self.execute(directory: directory, runtime: runtime, action: "--status")
            guard directory == project && !busy else { return }
            if showConfigurationFailure(result, directory: directory) { return }
            if result.0 == 0, let data = result.1.trimmingCharacters(in: .whitespacesAndNewlines).data(using: .utf8),
               let value = try? JSONDecoder().decode(RuntimeStatus.self, from: data) {
                status = value
                if error == nil { message = value.ready ? "服务已运行" : "服务未启动" }
            } else if error == nil {
                message = "暂未取得服务状态"
            }
            let goalResult = await Self.goalCommand(runtime: runtime, action: "--status")
            if goalResult.0 == 0, let data = goalResult.1.data(using: .utf8),
               let value = try? JSONDecoder().decode(GoalChannelStatus.self, from: data) { goalChannel = value }
        }
    }

    func run(action: String?) {
        guard !busy else { return }
        configurationBlocked = false
        busy = true; error = nil
        message = action == "--stop" ? "正在停止服务…" : "正在启动服务…"
        let directory = project
        Task {
            let result = await Self.execute(directory: directory, runtime: runtime, action: action)
            busy = false
            if result.0 == 0 {
                message = action == "--stop" ? "服务已停止" : "服务已启动，可以从手机继续工作"
            } else if !showConfigurationFailure(result, directory: directory) {
                message = "操作未完成"
                error = "当前配置目录\n\(directory.path)\n\n" + String(result.1.suffix(1200))
            }
            refresh()
        }
    }

    private func showConfigurationFailure(_ result: (Int32, String), directory: URL) -> Bool {
        guard result.0 == EX_NOINPUT || result.0 == EX_NOPERM else { return false }
        configurationBlocked = true
        status = RuntimeStatus()
        message = result.0 == EX_NOINPUT ? "未找到启动配置" : "无法读取启动配置"
        let hint = result.0 == EX_NOINPUT ? "点击“配置文件夹…”以选择原来的位置。"
            : "如有 macOS 权限提示，请确认后重试。也可以用“配置文件夹…”打开保存的位置。"
        error = "当前配置目录\n\(directory.path)\n\n\(hint)\n" + String(result.1.suffix(1000))
        return true
    }

    func chooseProject() {
        guard !busy && !updating else { return }
        let panel = NSOpenPanel()
        panel.title = "AIRemote 配置文件夹"
        panel.message = "当前配置位置\n\(project.path)\n\n可以使用此文件夹，或选择另一处已有的配置目录。"
        panel.canChooseFiles = false; panel.canChooseDirectories = true; panel.allowsMultipleSelection = false
        panel.directoryURL = project
        guard panel.runModal() == .OK, let chosen = panel.url else { return }
        guard FileManager.default.fileExists(atPath: chosen.appendingPathComponent(".local/mac-runtime.json").path) else {
            error = "这个文件夹中读不到 AIRemote 启动配置。\n\(chosen.path)"; return
        }
        if chosen != project { status = RuntimeStatus() }
        UserDefaults.standard.set(chosen.path, forKey: "ProjectDirectory")
        error = nil
        if configurationBlocked { run(action: nil) } else { refresh() }
    }

    func openWeb() {
        guard let url = URL(string: status.relay), url.scheme == "https" else { return }
        NSWorkspace.shared.open(url)
    }

    func pairMac(origin: String, code: String, name: String, workspace: URL) async -> String? {
        guard !busy && !updating else { return "正在处理其他操作，请稍后重试。" }
        busy = true; error = nil
        message = "正在配对电脑…"
        var startAfter = false
        defer {
            busy = false
            if startAfter { run(action: nil) }
        }
        let values = ["origin": origin.trimmingCharacters(in: .whitespacesAndNewlines),
                      "code": code.trimmingCharacters(in: .whitespacesAndNewlines),
                      "name": name, "workspace": workspace.path]
        guard let input = try? JSONSerialization.data(withJSONObject: values), input.count < 8192 else {
            message = "配对未完成"
            return "请输入有效的网址、配对码和项目目录。"
        }
        let previous = project
        let result = await Self.process([runtime.appendingPathComponent("deploy/pair_mac.py").path], timeout: 33, input: input)
        guard result.0 == 0, let data = result.1.data(using: .utf8),
              let response = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let directory = response["directory"] as? String else {
            message = "配对未完成"
            return Self.updateError(result.1)
        }
        if directory != previous.path && !status.running.isEmpty {
            let stopped = await Self.execute(directory: previous, runtime: runtime, action: "--stop")
            if stopped.0 != 0 {
                message = "配对成功，等待切换服务"
                return "处理旧服务时遇到问题，请用同一个配对码重试。\n" + String(stopped.1.suffix(1000))
            }
        }
        UserDefaults.standard.set(directory, forKey: "ProjectDirectory")
        status = RuntimeStatus()
        startAfter = true
        return nil
    }

    func openLogs() {
        let url = URL(fileURLWithPath: status.logsDirectory ?? project.appendingPathComponent(".local/logs").path)
        NSWorkspace.shared.open(url)
    }

    func launchGoalChannel() {
        guard !busy else { return }
        busy = true; error = nil
        goalMessage = "正在准备目标控制通道…"
        Task {
            let result = await Self.goalCommand(runtime: runtime, action: "--launch")
            busy = false
            if result.0 == 0, let data = result.1.data(using: .utf8),
               let value = try? JSONDecoder().decode(GoalChannelStatus.self, from: data) {
                goalMessage = value.message ?? "通道已准备好"
            } else { goalMessage = "目标通道未启动"; error = String(result.1.suffix(1200)) }
            refresh()
        }
    }

    func checkIfDue() {
        let checked = UserDefaults.standard.double(forKey: "LastUpdateCheck")
        if autoCheck && Date().timeIntervalSince1970 - checked > 6 * 3600 { checkUpdates(manual: false) }
    }

    func checkUpdates(manual: Bool = true, announce: Bool = false) {
        guard !updating else { return }
        updating = true; updateProgress = UpdateProgress(stage: "checking")
        updateMessage = "正在检查 GitHub 新版本…"
        if manual { error = nil }
        Task {
            let result = await Self.updateCommand(runtime: runtime, arguments: ["--check", "--current-version", version, "--progress"]) { [weak self] value in
                self?.updateProgress = value; self?.updateMessage = value.title
            }
            updating = false; updateProgress = nil
            if result.0 == 0, let data = result.1.data(using: .utf8), let value = try? JSONDecoder().decode(UpdateStatus.self, from: data) {
                update = value
                UserDefaults.standard.set(Date().timeIntervalSince1970, forKey: "LastUpdateCheck")
                updateMessage = value.available ? "新版本 \(value.latestVersion) 可用" : "已是最新版本 \(version)"
                if announce && autoCheck && value.available { announceUpdate = true }
            } else {
                UserDefaults.standard.set(Date().timeIntervalSince1970 - 5 * 3600, forKey: "LastUpdateCheck")
                updateMessage = "检查更新未完成，请稍后重试"
                if manual { error = Self.updateError(result.1) }
            }
        }
    }

    func installUpdate() {
        guard !updating && !busy else { return }
        updating = true; error = nil; updateProgress = UpdateProgress(stage: "connecting")
        updateMessage = updateProgress!.title
        let arguments = ["--prepare", "--config", config.path, "--app", Bundle.main.bundleURL.path, "--current-version", version, "--progress"]
        Task {
            let prepared = await Self.updateCommand(runtime: runtime, arguments: arguments) { [weak self] value in
                self?.updateProgress = value; self?.updateMessage = value.title
            }
            guard prepared.0 == 0, let data = prepared.1.data(using: .utf8),
                  let value = try? JSONDecoder().decode(UpdateStatus.self, from: data), let plan = value.plan else {
                updating = false; updateProgress = nil; updateMessage = "更新未安装"
                error = Self.updateError(prepared.1); return
            }
            updateProgress = UpdateProgress(stage: "installing"); updateMessage = updateProgress!.title
            let launched = await Self.updateCommand(runtime: runtime, arguments: ["--launch-install", plan, "--wait-pid", String(ProcessInfo.processInfo.processIdentifier)])
            if launched.0 == 0 { NSApplication.shared.terminate(nil) }
            else { updating = false; updateProgress = nil; updateMessage = "更新未安装"; error = Self.updateError(launched.1) }
        }
    }

    nonisolated private static func updateCommand(runtime: URL, arguments: [String], progress: (@MainActor @Sendable (UpdateProgress) -> Void)? = nil) async -> (Int32, String) {
        await UpdaterProcess.run([runtime.appendingPathComponent("deploy/update_mac.py").path] + arguments, progress: progress)
    }

    nonisolated private static func updateError(_ text: String) -> String {
        if let data = text.data(using: .utf8), let value = try? JSONSerialization.jsonObject(with: data) as? [String: Any], let message = value["error"] as? String { return message }
        return String(text.suffix(1200))
    }

    nonisolated private static func goalCommand(runtime: URL, action: String) async -> (Int32, String) {
        await process([runtime.appendingPathComponent("deploy/setup_goal_channel.py").path, action], timeout: 20)
    }

    nonisolated private static func execute(directory: URL, runtime: URL, action: String?) async -> (Int32, String) {
        await process([runtime.appendingPathComponent("deploy/start_mac.py").path, "--config", directory.appendingPathComponent(".local/mac-runtime.json").path,
            "--runtime-root", runtime.path] + (action.map { [$0] } ?? []), timeout: action == "--status" ? 15 : 75)
    }

    nonisolated private static func process(_ arguments: [String], timeout: TimeInterval, input: Data? = nil) async -> (Int32, String) {
        await Task.detached(priority: .utility) {
            let process = Process()
            let pipe = Pipe()
            process.executableURL = URL(fileURLWithPath: "/usr/bin/python3")
            process.arguments = arguments
            let stdin = input.map { _ in Pipe() }
            if let stdin { process.standardInput = stdin }
            else { process.standardInput = FileHandle.nullDevice }
            process.standardOutput = pipe; process.standardError = pipe
            do {
                try process.run()
                let expiration = DispatchWorkItem { if process.isRunning { process.terminate() } }
                DispatchQueue.global(qos: .utility).asyncAfter(deadline: .now() + timeout, execute: expiration)
                defer { expiration.cancel() }
                if let stdin, let input {
                    try stdin.fileHandleForWriting.write(contentsOf: input)
                    try stdin.fileHandleForWriting.close()
                }
                let output = pipe.fileHandleForReading.readDataToEndOfFile()
                process.waitUntilExit()
                if process.terminationStatus == 15 || process.terminationStatus == -15 {
                    return (-1, "操作超时，请检查是否有系统权限提示，然后重试。")
                }
                return (process.terminationStatus, String(data: output, encoding: .utf8) ?? "无法读取启动结果")
            } catch {
                return (-1, "无法执行操作：\(error.localizedDescription)\n请检查 Python 和配置文件夹。")
            }
        }.value
    }
}

struct ControlView: View {
    @ObservedObject var model: ServiceModel
    @State private var pairing = false

    var body: some View {
        VStack(alignment: .leading, spacing: 22) {
            HStack(spacing: 13) {
                if let url = Bundle.main.url(forResource: "AIRemote", withExtension: "png"), let icon = NSImage(contentsOf: url) {
                    Image(nsImage: icon).resizable().frame(width: 50, height: 50)
                }
                VStack(alignment: .leading, spacing: 4) {
                    Text("AIRemote").font(.system(size: 25, weight: .semibold))
                    Text("连接你的 Mac AI 客户端").font(.system(size: 12)).foregroundStyle(.secondary)
                }
                Spacer()
            }
            VStack(alignment: .leading, spacing: 13) {
                HStack(spacing: 9) {
                    if model.busy { ProgressView().controlSize(.small) }
                    else { Circle().fill(model.status.ready ? Color.green : Color.gray.opacity(0.6)).frame(width: 8, height: 8) }
                    Text(model.message).font(.system(size: 13, weight: .medium))
                }
                ForEach(model.status.providers) { client in
                    HStack(spacing: 9) {
                        if let url = Bundle.main.url(forResource: client.id, withExtension: "png"), let icon = NSImage(contentsOf: url) {
                            Image(nsImage: icon).resizable().scaledToFit().frame(width: 23, height: 23)
                        }
                        Text(client.name).font(.system(size: 13))
                        Spacer()
                        Text(client.statusLabel).font(.system(size: 12)).foregroundStyle(client.connected ? .green : .secondary)
                    }
                }
            }
            .padding(16).background(Color.primary.opacity(0.035)).clipShape(RoundedRectangle(cornerRadius: 13))

            HStack(spacing: 10) {
                Button { model.run(action: nil) } label: {
                    Label("启动服务", systemImage: "play.fill").frame(maxWidth: .infinity)
                }.buttonStyle(.borderedProminent).disabled(model.busy || model.status.ready)
                Button { model.run(action: "--stop") } label: {
                    Label("停止服务", systemImage: "stop.fill").frame(maxWidth: .infinity)
                }.buttonStyle(.bordered).disabled(model.busy || model.status.running.isEmpty)
            }.controlSize(.large)
            HStack(spacing: 10) {
                Button("打开手机网页") { model.openWeb() }.frame(maxWidth: .infinity)
                Button("查看日志") { model.openLogs() }.frame(maxWidth: .infinity)
            }.buttonStyle(.bordered).controlSize(.large)
            Button { pairing = true } label: {
                Label("配对电脑…", systemImage: "link").frame(maxWidth: .infinity)
            }.buttonStyle(.bordered).disabled(model.busy || model.updating)
            VStack(alignment: .leading, spacing: 7) {
                Button { model.launchGoalChannel() } label: {
                    Label(model.goalChannel.connected ? "Codex 目标通道已连接" : "通过目标通道启动 Codex", systemImage: "target")
                        .frame(maxWidth: .infinity)
                }.buttonStyle(.bordered).disabled(model.busy || model.goalChannel.connected)
                Text(model.goalMessage.isEmpty ? "首次启用：先正常退出 Codex，再用此按钮打开。原有对话会保留。" : model.goalMessage)
                    .font(.system(size: 11)).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            }
            Divider()
            HStack {
                Text("版本 \(model.version)").font(.system(size: 12)).foregroundStyle(.secondary)
                Spacer()
                Button(model.updating ? "更新处理中…" : "检查更新") { model.checkUpdates() }.disabled(model.updating)
                if model.update?.available == true {
                    Button("更新并重启") { model.installUpdate() }.buttonStyle(.borderedProminent).disabled(model.updating || model.busy)
                }
            }
            Toggle("自动检查 GitHub 更新", isOn: $model.autoCheck).toggleStyle(.checkbox).font(.system(size: 12))
            if model.updating, let progress = model.updateProgress {
                VStack(alignment: .leading, spacing: 7) {
                    if let fraction = progress.fraction { ProgressView(value: fraction).accessibilityLabel("更新下载进度").accessibilityValue("\(Int(fraction * 100))%") }
                    else { ProgressView().progressViewStyle(.linear).accessibilityLabel("更新处理进度") }
                    Text(model.updateMessage).font(.system(size: 12, weight: .medium)).accessibilityAddTraits(.updatesFrequently)
                    if let detail = progress.detail { Text(detail).font(.system(size: 11)).monospacedDigit().foregroundStyle(.secondary) }
                }
            } else if !model.updateMessage.isEmpty { Text(model.updateMessage).font(.system(size: 11)).foregroundStyle(.secondary) }
            if let error = model.error {
                ScrollView { Text(error).font(.system(size: 11)).foregroundStyle(.red).textSelection(.enabled).frame(maxWidth: .infinity, alignment: .leading) }
                    .frame(maxHeight: 110)
            }
            HStack(alignment: .top) {
                Text("关闭窗口会收至菜单栏，服务继续运行。\n需要停止时，点击“停止服务”。").font(.system(size: 11)).foregroundStyle(.secondary).lineSpacing(4).fixedSize(horizontal: false, vertical: true)
                Spacer()
                Button("配置文件夹…") { model.chooseProject() }
                    .controlSize(.small).help(model.project.path).disabled(model.busy || model.updating)
            }
        }
        .padding(28).frame(width: 420)
        .sheet(isPresented: $pairing) { PairMacView(model: model) }
        .alert("发现 AIRemote 新版本", isPresented: Binding(
            get: { model.announceUpdate && !model.busy && !model.updating },
            set: { model.announceUpdate = $0 }
        ), presenting: model.update) { _ in
            Button("更新并重启") { model.installUpdate() }
            Button("稍后", role: .cancel) {}
        } message: { update in
            Text("当前版本 \(model.version)，可更新到 \(update.latestVersion)。")
        }
        .onAppear { model.launch() }
    }
}

struct PairMacView: View {
    @ObservedObject var model: ServiceModel
    @Environment(\.dismiss) private var dismiss
    @State private var origin = ""
    @State private var code = ""
    @State private var name = Host.current().localizedName ?? "我的 Mac"
    @State private var workspace: URL?
    @State private var error: String?

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text("配对电脑").font(.title2.weight(.semibold))
            Text("在网页的「账号与设备」生成配对码。配对将此 Mac 上的 AI 对话接入该账号。")
                .foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            Grid(alignment: .leading, horizontalSpacing: 12, verticalSpacing: 12) {
                GridRow { Text("网站地址"); TextField("https://remote.example.com", text: $origin).labelsHidden() }
                GridRow { Text("配对码"); SecureField("粘贴一次性配对码", text: $code).labelsHidden() }
                GridRow { Text("设备名称"); TextField("在账号管理中显示", text: $name).labelsHidden() }
                GridRow {
                    Text("项目文件夹")
                    Button(workspace?.lastPathComponent ?? "选择文件夹…") {
                        let panel = NSOpenPanel()
                        panel.message = "选择允许下载文件和新建对话的项目目录。"
                        panel.canChooseFiles = false; panel.canChooseDirectories = true
                        if panel.runModal() == .OK { workspace = panel.url }
                    }.help(workspace?.path ?? "授权一个项目目录")
                }
            }.disabled(model.busy)
            if let error { Text(error).foregroundStyle(.red).textSelection(.enabled) }
            HStack {
                Spacer()
                Button("取消") { dismiss() }.keyboardShortcut(.cancelAction).disabled(model.busy)
                Button(model.busy ? "正在配对…" : "配对并启动") {
                    guard let workspace else { return }
                    Task {
                        error = await model.pairMac(origin: origin, code: code, name: name, workspace: workspace)
                        if error == nil { code = ""; dismiss() }
                    }
                }.keyboardShortcut(.defaultAction).disabled(model.busy || origin.isEmpty || code.isEmpty || name.isEmpty || workspace == nil)
            }
        }.padding(25).frame(width: 440).interactiveDismissDisabled(model.busy)
            .onAppear { origin = model.status.relay }
    }
}

private let controlWindowID = "control"

private struct ControlWindowDockVisibility: NSViewRepresentable {
    func makeNSView(context: Context) -> Observer { Observer() }
    func updateNSView(_ nsView: Observer, context: Context) {}

    final class Observer: NSView {
        override func viewDidMoveToWindow() {
            super.viewDidMoveToWindow()
            let notifications = NotificationCenter.default
            notifications.removeObserver(self, name: NSWindow.willCloseNotification, object: nil)
            guard let window else { return }
            NSApp.setActivationPolicy(.regular)
            notifications.addObserver(self, selector: #selector(windowWillClose),
                                      name: NSWindow.willCloseNotification, object: window)
        }

        @objc private func windowWillClose() {
            NSApp.setActivationPolicy(.accessory)
        }
    }
}

private struct MenuBarActions: View {
    @ObservedObject var model: ServiceModel
    @Environment(\.openWindow) private var openWindow

    var body: some View {
        Button("打开 AIRemote") {
            NSApp.setActivationPolicy(.regular)
            openWindow(id: controlWindowID)
            if #available(macOS 14.0, *) { NSApp.activate() }
            else { NSApp.activate(ignoringOtherApps: true) }
        }
        Text(model.message)
        Divider()
        Button("退出 AIRemote") { NSApp.terminate(nil) }
    }
}

@main struct AIRemoteApplication: App {
    @StateObject private var model = ServiceModel()

    private static let menuBarIcon: NSImage = {
        let image = NSImage(size: NSSize(width: 18, height: 18), flipped: true) { _ in
            NSColor.black.setStroke()
            NSColor.black.setFill()
            let mark = NSBezierPath()
            mark.lineWidth = 1.8
            mark.lineCapStyle = .round
            mark.lineJoinStyle = .round
            mark.move(to: NSPoint(x: 2.5, y: 14))
            mark.line(to: NSPoint(x: 7.5, y: 5.7))
            mark.curve(to: NSPoint(x: 10.5, y: 5.7), controlPoint1: NSPoint(x: 8.2, y: 4.5), controlPoint2: NSPoint(x: 9.8, y: 4.5))
            mark.line(to: NSPoint(x: 15.5, y: 14))
            mark.move(to: NSPoint(x: 5.2, y: 10))
            mark.line(to: NSPoint(x: 12.8, y: 10))
            mark.stroke()
            NSBezierPath(ovalIn: NSRect(x: 13.1, y: 2, width: 3, height: 3)).fill()
            return true
        }
        image.isTemplate = true
        return image
    }()

    var body: some Scene {
        Window("AIRemote", id: controlWindowID) {
            ControlView(model: model)
                .background(ControlWindowDockVisibility())
        }
        .windowResizability(.contentSize)
        .commands { CommandGroup(replacing: .newItem) {} }

        MenuBarExtra {
            MenuBarActions(model: model)
        } label: {
            Image(nsImage: Self.menuBarIcon).accessibilityLabel("AIRemote")
        }
        .menuBarExtraStyle(.menu)
    }
}
