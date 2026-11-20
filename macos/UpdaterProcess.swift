import Foundation

struct UpdateProgress: Decodable, Sendable {
    var event = "update-progress"
    var stage: String
    var downloadedBytes: Int64?
    var totalBytes: Int64?
    var bytesPerSecond: Double?
    var attempt: Int?

    var fraction: Double? {
        guard stage == "downloading", let total = totalBytes, total > 0 else { return nil }
        return min(1, max(0, Double(downloadedBytes ?? 0) / Double(total)))
    }
    var title: String {
        switch stage {
        case "checking": return "正在检查 GitHub 新版本…"
        case "connecting": return "正在连接 GitHub 下载源…"
        case "retrying": return "连接中断，正在重试（第 \(attempt ?? 2) 次）…"
        case "downloading": return fraction.map { "正在下载更新 · \(Int($0 * 100))%" } ?? "正在下载更新…"
        case "verifying": return "下载完成，正在校验文件…"
        case "extracting": return "正在解压更新…"
        case "verifying_app": return "正在验证 Mac 应用…"
        case "prepared": return "更新已准备好"
        case "waiting_for_idle": return "正在确认任务是否空闲…"
        case "installing": return "正在安装并重新启动…"
        default: return "正在更新…"
        }
    }
    var detail: String? {
        guard stage == "downloading" else { return nil }
        let downloaded = Self.bytes(downloadedBytes ?? 0)
        let amount = totalBytes.flatMap { $0 > 0 ? "\(downloaded) / \(Self.bytes($0))" : nil } ?? "已下载 \(downloaded)"
        return amount + ((bytesPerSecond ?? 0) > 0 ? " · \(Self.bytes(Int64(bytesPerSecond!)))/s" : "")
    }
    static func bytes(_ value: Int64) -> String {
        let value = max(0, value)
        if value < 1024 { return "\(value) B" }
        if value < 1024 * 1024 { return String(format: "%.1f KB", Double(value) / 1024) }
        return String(format: "%.1f MB", Double(value) / (1024 * 1024))
    }
}

enum UpdaterProcess {
    // Await every progress delivery before consuming the final result. Late
    // callbacks cannot overwrite "latest version" or failure messages.
    static func run(_ arguments: [String], timeout: TimeInterval = 300,
                    progress: (@MainActor @Sendable (UpdateProgress) -> Void)? = nil) async -> (Int32, String) {
        await Task.detached(priority: .utility) {
            let process = Process(), pipe = Pipe()
            process.executableURL = URL(fileURLWithPath: "/usr/bin/python3")
            process.arguments = arguments
            process.standardInput = FileHandle.nullDevice
            process.standardOutput = pipe; process.standardError = pipe
            do {
                try process.run()
                let expiration = DispatchWorkItem { if process.isRunning { process.terminate() } }
                DispatchQueue.global(qos: .utility).asyncAfter(deadline: .now() + timeout, execute: expiration)
                defer { expiration.cancel() }
                var result = ""
                for try await line in pipe.fileHandleForReading.bytes.lines {
                    if let data = line.data(using: .utf8),
                       let value = try? JSONDecoder().decode(UpdateProgress.self, from: data), value.event == "update-progress" {
                        await progress?(value)
                    } else {
                        result += line + "\n"
                        if result.utf8.count > 65536 { result = String(result.suffix(16000)) }
                    }
                }
                process.waitUntilExit()
                if process.terminationStatus == 15 || process.terminationStatus == -15 {
                    return (-1, "更新操作超时，请检查网络后重试。")
                }
                return (process.terminationStatus, result.trimmingCharacters(in: .whitespacesAndNewlines))
            } catch { return (-1, "无法读取更新结果：\(error.localizedDescription)") }
        }.value
    }
}
