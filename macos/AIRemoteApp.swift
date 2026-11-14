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

@MainActor final class ServiceModel: ObservableObject {
    @Published var status = RuntimeStatus()
    @Published var busy = false
    @Published var message = "正在检查服务…"
    @Published var error: String?
    private var checking = false
    private var launched = false

    var project: URL {
        let saved = UserDefaults.standard.string(forKey: "ProjectDirectory")
        let bundled = Bundle.main.object(forInfoDictionaryKey: "AIRemoteProjectDirectory") as? String
        return URL(fileURLWithPath: saved ?? bundled ?? "", isDirectory: true)
    }
    var config: URL { project.appendingPathComponent(".local/mac-runtime.json") }

    func launch() {
        guard !launched else { return }
        launched = true
        run(action: nil)
    }

    func refresh() {
        guard !busy && !checking else { return }
        checking = true
        let directory = project
        Task {
            defer { checking = false }
            let result = await Self.execute(directory: directory, action: "--status")
            if result.0 == 0, let data = result.1.trimmingCharacters(in: .whitespacesAndNewlines).data(using: .utf8),
               let value = try? JSONDecoder().decode(RuntimeStatus.self, from: data) {
                status = value
                if error == nil { message = value.ready ? "服务已运行" : "服务未启动" }
            } else if error == nil {
                message = "暂未取得服务状态"
            }
        }
    }

    func run(action: String?) {
        guard !busy else { return }
        busy = true; error = nil
        message = action == "--stop" ? "正在停止服务…" : "正在启动服务…"
        let directory = project
        Task {
            let result = await Self.execute(directory: directory, action: action)
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

    func chooseProject() {
        let panel = NSOpenPanel()
        panel.title = "选择 AIRemote 项目文件夹"
        panel.message = "选择包含 .local/mac-runtime.json 的 AIRemote 文件夹。"
        panel.canChooseFiles = false; panel.canChooseDirectories = true; panel.allowsMultipleSelection = false
        panel.directoryURL = project
        guard panel.runModal() == .OK, let chosen = panel.url else { return }
        guard FileManager.default.fileExists(atPath: chosen.appendingPathComponent(".local/mac-runtime.json").path) else {
            error = "这个文件夹中没有 AIRemote 启动配置。"; return
        }
        UserDefaults.standard.set(chosen.path, forKey: "ProjectDirectory")
        error = nil; refresh()
    }

    func openWeb() {
        guard let url = URL(string: status.relay), url.scheme == "https" else { return }
        NSWorkspace.shared.open(url)
    }

    func openLogs() {
        let url = URL(fileURLWithPath: status.logsDirectory ?? project.appendingPathComponent(".local/logs").path)
        NSWorkspace.shared.open(url)
    }

    nonisolated private static func execute(directory: URL, action: String?) async -> (Int32, String) {
        await Task.detached(priority: .utility) {
            let process = Process()
            let pipe = Pipe()
            process.executableURL = URL(fileURLWithPath: "/usr/bin/python3")
            process.arguments = [directory.appendingPathComponent("deploy/start_mac.py").path, "--config", directory.appendingPathComponent(".local/mac-runtime.json").path] + (action.map { [$0] } ?? [])
            process.currentDirectoryURL = directory
            process.standardInput = FileHandle.nullDevice
            process.standardOutput = pipe; process.standardError = pipe
            do {
                try process.run()
                let output = pipe.fileHandleForReading.readDataToEndOfFile()
                process.waitUntilExit()
                return (process.terminationStatus, String(data: output, encoding: .utf8) ?? "无法读取启动结果")
            } catch {
                return (-1, "无法启动服务：\(error.localizedDescription)\n请检查项目位置或使用下方按钮重新选择项目文件夹。")
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
