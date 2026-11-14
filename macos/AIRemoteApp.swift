import SwiftUI
import AppKit

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
    @Published var updating = false
    @Published var updateMessage = ""
    @Published var goalChannel = GoalChannelStatus()
    @Published var goalMessage = ""
    @Published var autoCheck = UserDefaults.standard.object(forKey: "AutoCheckUpdates") as? Bool ?? true {
        didSet { UserDefaults.standard.set(autoCheck, forKey: "AutoCheckUpdates") }
    }
    private var checking = false
    private var launched = false

    var project: URL {
        let saved = UserDefaults.standard.string(forKey: "ProjectDirectory")
        let bundled = Bundle.main.object(forInfoDictionaryKey: "AIRemoteProjectDirectory") as? String
        let fallback = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Library/Application Support/AIRemote").path
        return URL(fileURLWithPath: saved ?? bundled ?? fallback, isDirectory: true)
    }
    var config: URL { project.appendingPathComponent(".local/mac-runtime.json") }
    var version: String { Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? "0.0.0" }
    var runtime: URL { Bundle.main.resourceURL!.appendingPathComponent("runtime", isDirectory: true) }
    var needsFolderAccess: Bool {
        let home = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Library/Application Support/AIRemote").path
        return project.path != home && UserDefaults.standard.string(forKey: "FolderAccessVersion") != version + ":" + project.path
    }

    func launch() {
        guard !launched else { return }
        launched = true
        checkIfDue()
        if needsFolderAccess {
            message = "请选择原配置文件夹，确认此版本的访问权限"
            chooseProject(startAfter: true)
        } else { run(action: nil) }
    }

    func refresh() {
        checkIfDue()
        guard !busy && !checking && !needsFolderAccess else { return }
        checking = true
        let directory = project
        Task {
            defer { checking = false }
            let result = await Self.execute(directory: directory, runtime: runtime, action: "--status")
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
        if needsFolderAccess { chooseProject(startAfter: true); return }
        busy = true; error = nil
        message = action == "--stop" ? "正在停止服务…" : "正在启动服务…"
        let directory = project
        Task {
            let result = await Self.execute(directory: directory, runtime: runtime, action: action)
            busy = false
            if result.0 == 0 {
                message = action == "--stop" ? "服务已停止" : "服务已启动，可以从手机继续工作"
            } else {
                message = "操作未完成"
                error = String(result.1.suffix(1600))
            }
            refresh()
        }
    }

    func chooseProject(startAfter: Bool = false) {
        let panel = NSOpenPanel()
        panel.title = "选择 AIRemote 项目文件夹"
        panel.message = "选择包含 .local/mac-runtime.json 的原配置文件夹。应用更新后，macOS 需要重新确认此目录的访问权限。"
        panel.canChooseFiles = false; panel.canChooseDirectories = true; panel.allowsMultipleSelection = false
        panel.directoryURL = project
        guard panel.runModal() == .OK, let chosen = panel.url else { return }
        guard FileManager.default.fileExists(atPath: chosen.appendingPathComponent(".local/mac-runtime.json").path) else {
            error = "这个文件夹中没有 AIRemote 启动配置。"; return
        }
        UserDefaults.standard.set(chosen.path, forKey: "ProjectDirectory")
        UserDefaults.standard.set(version + ":" + chosen.path, forKey: "FolderAccessVersion")
        error = nil
        if startAfter { run(action: nil) } else { refresh() }
    }

    func openWeb() {
        guard let url = URL(string: status.relay), url.scheme == "https" else { return }
        NSWorkspace.shared.open(url)
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

    func checkUpdates(manual: Bool = true) {
        guard !updating else { return }
        updating = true
        if manual { updateMessage = "正在检查 GitHub…" }
        Task {
            let result = await Self.updateCommand(runtime: runtime, arguments: ["--check", "--current-version", version])
            updating = false
            if result.0 == 0, let data = result.1.data(using: .utf8), let value = try? JSONDecoder().decode(UpdateStatus.self, from: data) {
                update = value
                UserDefaults.standard.set(Date().timeIntervalSince1970, forKey: "LastUpdateCheck")
                updateMessage = value.available ? "新版本 \(value.latestVersion) 可用" : "已是最新版本"
            } else {
                UserDefaults.standard.set(Date().timeIntervalSince1970 - 5 * 3600, forKey: "LastUpdateCheck")
                if manual { updateMessage = "暂时无法检查更新，请稍后重试" }
            }
        }
    }

    func installUpdate() {
        guard !updating && !busy else { return }
        updating = true; updateMessage = "正在下载并校验更新…"
        let arguments = ["--prepare", "--config", config.path, "--app", Bundle.main.bundleURL.path, "--current-version", version]
        Task {
            let prepared = await Self.updateCommand(runtime: runtime, arguments: arguments)
            guard prepared.0 == 0, let data = prepared.1.data(using: .utf8),
                  let value = try? JSONDecoder().decode(UpdateStatus.self, from: data), let plan = value.plan else {
                updating = false; updateMessage = "更新未安装"
                error = String(prepared.1.suffix(1200)); return
            }
            updateMessage = "校验通过，正在重新启动…"
            let launched = await Self.updateCommand(runtime: runtime, arguments: ["--launch-install", plan, "--wait-pid", String(ProcessInfo.processInfo.processIdentifier)])
            if launched.0 == 0 { NSApplication.shared.terminate(nil) }
            else { updating = false; updateMessage = "更新未安装"; error = String(launched.1.suffix(1200)) }
        }
    }

    nonisolated private static func updateCommand(runtime: URL, arguments: [String]) async -> (Int32, String) {
        await process([runtime.appendingPathComponent("deploy/update_mac.py").path] + arguments, timeout: 300)
    }

    nonisolated private static func goalCommand(runtime: URL, action: String) async -> (Int32, String) {
        await process([runtime.appendingPathComponent("deploy/setup_goal_channel.py").path, action], timeout: 20)
    }

    nonisolated private static func execute(directory: URL, runtime: URL, action: String?) async -> (Int32, String) {
        await process([runtime.appendingPathComponent("deploy/start_mac.py").path, "--config", directory.appendingPathComponent(".local/mac-runtime.json").path,
            "--runtime-root", runtime.path] + (action.map { [$0] } ?? []), timeout: action == "--status" ? 15 : 75)
    }

    nonisolated private static func process(_ arguments: [String], timeout: TimeInterval) async -> (Int32, String) {
        await Task.detached(priority: .utility) {
            let process = Process()
            let pipe = Pipe()
            process.executableURL = URL(fileURLWithPath: "/usr/bin/python3")
            process.arguments = arguments
            process.standardInput = FileHandle.nullDevice
            process.standardOutput = pipe; process.standardError = pipe
            do {
                try process.run()
                let expiration = DispatchWorkItem { if process.isRunning { process.terminate() } }
                DispatchQueue.global(qos: .utility).asyncAfter(deadline: .now() + timeout, execute: expiration)
                defer { expiration.cancel() }
                let output = pipe.fileHandleForReading.readDataToEndOfFile()
                process.waitUntilExit()
                if process.terminationStatus == 15 || process.terminationStatus == -15 {
                    return (-1, "操作超时。请用右下角按钮重新选择原配置文件夹，确认目录访问权限后重试。")
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
    let timer = Timer.publish(every: 5, on: .main, in: .common).autoconnect()

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
                if model.updating { ProgressView().controlSize(.small) }
                Button("检查更新") { model.checkUpdates() }.disabled(model.updating)
                if model.update?.available == true {
                    Button("更新并重启") { model.installUpdate() }.buttonStyle(.borderedProminent).disabled(model.updating || model.busy)
                }
            }
            Toggle("自动检查 GitHub 更新", isOn: $model.autoCheck).toggleStyle(.checkbox).font(.system(size: 12))
            if !model.updateMessage.isEmpty { Text(model.updateMessage).font(.system(size: 11)).foregroundStyle(.secondary) }
            if let error = model.error {
                ScrollView { Text(error).font(.system(size: 11)).foregroundStyle(.red).textSelection(.enabled).frame(maxWidth: .infinity, alignment: .leading) }
                    .frame(maxHeight: 110)
            }
            HStack(alignment: .top) {
                Text("关闭窗口后服务继续运行。\n需要停止时，点击“停止服务”。").font(.system(size: 11)).foregroundStyle(.secondary).lineSpacing(4).fixedSize(horizontal: false, vertical: true)
                Spacer()
                Button { model.chooseProject() } label: { Image(systemName: "folder.badge.gearshape") }.buttonStyle(.plain).help("选择项目文件夹").disabled(model.busy)
            }
        }
        .padding(28).frame(width: 420)
        .onAppear { model.launch() }
        .onReceive(timer) { _ in model.refresh() }
    }
}

@main struct AIRemoteApplication: App {
    @StateObject private var model = ServiceModel()
    var body: some Scene {
        WindowGroup("AIRemote") { ControlView(model: model) }
            .windowResizability(.contentSize)
            .commands { CommandGroup(replacing: .newItem) {} }
    }
}
