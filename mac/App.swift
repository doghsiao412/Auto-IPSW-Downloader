// Auto IPSW Downloader for Mac: a native window over the background service.
// The download engine is the Python service this app installs; closing the window never stops downloads.
import AppKit
import SwiftUI

let serviceURL = URL(string: "http://127.0.0.1:18766")!
let supportFolder = FileManager.default.homeDirectoryForCurrentUser
    .appendingPathComponent("Library/Application Support/Auto IPSW Downloader")
let firmwareFolder = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Library/iTunes")
let keychainService = "auto-ipsw-downloader-smtp"

struct Transfer: Identifiable {
    let id: String
    let phase: String
    let progress: Double
    let total: Double
    let speed: Double
    let eta: Double?
    let reconnects: Int
}

struct Device: Identifiable, Hashable {
    let id: String
    let name: String
    var kind: String { id.hasPrefix("iPhone") ? "iPhone" : id.hasPrefix("iPad") ? "iPad" : "Mac" }
}

struct ServiceError: LocalizedError {
    let message: String
    var errorDescription: String? { message }
}

func runTool(_ path: String, _ arguments: [String], input: String? = nil) async -> (Int32, String) {
    await withCheckedContinuation { continuation in
        DispatchQueue.global().async {
            let process = Process()
            process.executableURL = URL(fileURLWithPath: path)
            process.arguments = arguments
            let output = Pipe(), stdin = Pipe()
            process.standardOutput = output
            process.standardError = output
            process.standardInput = stdin
            do { try process.run() } catch {
                continuation.resume(returning: (-1, error.localizedDescription))
                return
            }
            if let input { stdin.fileHandleForWriting.write(input.data(using: .utf8)!) }
            try? stdin.fileHandleForWriting.close()
            let data = output.fileHandleForReading.readDataToEndOfFile()
            process.waitUntilExit()
            continuation.resume(returning: (process.terminationStatus, String(data: data, encoding: .utf8) ?? ""))
        }
    }
}

func gigabytes(_ value: Double) -> String { String(format: "%.2f GB", value / 1e9) }

func clock(_ value: Double?) -> String {
    guard let value else { return "—" }
    let formatter = DateFormatter()
    formatter.locale = Locale(identifier: "zh_TW")
    formatter.dateFormat = "M/d HH:mm"
    return formatter.string(from: Date(timeIntervalSince1970: value))
}

func duration(_ seconds: Double?) -> String {
    guard let seconds, seconds.isFinite else { return "" }
    let minutes = Int(seconds / 60)
    return minutes >= 60 ? "約 \(minutes / 60) 小時 \(minutes % 60) 分" : minutes >= 1 ? "約 \(minutes) 分鐘" : "不到 1 分鐘"
}

@MainActor
final class Model: ObservableObject {
    enum Stage: Equatable { case starting, needsTools, installing, failed(String), ready }

    @Published var stage = Stage.starting
    @Published var enabled = false
    @Published var types: Set<String> = []
    @Published var models: [String] = []
    @Published var interval = 3.0
    @Published var cleanupEarly = false
    @Published var phase = ""
    @Published var message = ""
    @Published var latest: [String: String] = [:]
    @Published var planCount = 0
    @Published var planBytes = 0.0
    @Published var doneCount = 0
    @Published var transfers: [Transfer] = []
    @Published var free = 0.0
    @Published var lastCheck: Double?
    @Published var nextCheck: Double?
    @Published var notifyError = ""
    @Published var notice = ""
    @Published var online = true
    @Published var devices: [Device] = []
    @Published var mailReady = FileManager.default.fileExists(atPath: supportFolder.appendingPathComponent("data/mail.json").path)

    private var config: [String: Any] = [:]
    private var token = ""
    private var timer: Timer?
    private var failures = 0
    private let version = Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? ""
    private let build = Bundle.main.object(forInfoDictionaryKey: "CFBundleVersion") as? String ?? ""
    private var buildMarker: URL { supportFolder.appendingPathComponent("app/build") }

    private func resource(_ name: String) -> String { (Bundle.main.resourcePath ?? "") + "/" + name }

    func call(_ path: String, body: [String: Any]? = nil) async throws -> [String: Any] {
        var request = URLRequest(url: serviceURL.appendingPathComponent(path))
        request.timeoutInterval = 10
        request.cachePolicy = .reloadIgnoringLocalCacheData
        if let body {
            request.httpMethod = "POST"
            request.httpBody = try JSONSerialization.data(withJSONObject: body)
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
            request.setValue(token, forHTTPHeaderField: "X-CSRF-Token")
        }
        let (data, _) = try await URLSession.shared.data(for: request)
        let value = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] ?? [:]
        if let error = value["error"] as? String { throw ServiceError(message: error) }
        return value
    }

    func start() async {
        // Reinstall when the service is missing or comes from a different build of this app.
        let installed = (try? String(contentsOf: buildMarker, encoding: .utf8)) ?? ""
        if installed == build, let status = try? await call("api/status"), status["platform"] as? String == "mac" {
            apply(status)
            becomeReady()
            return
        }
        await install()
    }

    func install() async {
        let (tools, _) = await runTool("/usr/bin/xcode-select", ["-p"])
        guard tools == 0, FileManager.default.isExecutableFile(atPath: "/usr/bin/python3") else {
            stage = .needsTools
            return
        }
        stage = .installing
        let (code, output) = await runTool("/bin/sh", [resource("mac/install.sh")])
        guard code == 0 else {
            stage = .failed(output)
            return
        }
        try? build.write(to: buildMarker, atomically: true, encoding: .utf8)
        for _ in 0..<40 {
            if let status = try? await call("api/status") {
                apply(status)
                becomeReady()
                return
            }
            try? await Task.sleep(nanoseconds: 1_000_000_000)
        }
        stage = .failed("背景服務沒有啟動。紀錄檔在：\n" + supportFolder.appendingPathComponent("data/service.log").path)
    }

    func requestTools() async {
        _ = await runTool("/usr/bin/xcode-select", ["--install"])
    }

    private func becomeReady() {
        stage = .ready
        timer?.invalidate()
        timer = Timer.scheduledTimer(withTimeInterval: 2, repeats: true) { [weak self] _ in
            Task { @MainActor in await self?.refresh() }
        }
    }

    func refresh() async {
        do {
            apply(try await call("api/status"))
            failures = 0
            online = true
        } catch {
            failures += 1
            if failures >= 3 { online = false }
        }
    }

    private func apply(_ status: [String: Any]) {
        token = status["token"] as? String ?? token
        if let value = status["config"] as? [String: Any] {
            config = value
            enabled = value["enabled"] as? Bool ?? false
            types = Set(value["types"] as? [String] ?? [])
            models = value["models"] as? [String] ?? []
            interval = (value["interval_hours"] as? NSNumber)?.doubleValue ?? 3
            cleanupEarly = value["cleanup_early"] as? Bool ?? false
        }
        let state = status["state"] as? [String: Any] ?? [:]
        phase = state["phase"] as? String ?? ""
        message = state["message"] as? String ?? ""
        latest = state["latest"] as? [String: String] ?? [:]
        let plan = state["plan"] as? [[String: Any]] ?? []
        planCount = plan.count
        planBytes = plan.reduce(0) { $0 + (($1["size"] as? NSNumber)?.doubleValue ?? 0) }
        doneCount = phase == "syncing" ? (state["progress"] as? NSNumber)?.intValue ?? 0 : 0
        lastCheck = (state["last_check"] as? NSNumber)?.doubleValue
        nextCheck = (state["next_check"] as? NSNumber)?.doubleValue
        free = ((status["disk"] as? [String: Any])?["free"] as? NSNumber)?.doubleValue ?? 0
        notifyError = (status["notifications"] as? [String: Any])?["last_error"] as? String ?? ""
        transfers = (status["transfers"] as? [[String: Any]] ?? []).map {
            Transfer(id: $0["name"] as? String ?? "", phase: $0["phase"] as? String ?? "",
                     progress: ($0["progress"] as? NSNumber)?.doubleValue ?? 0,
                     total: ($0["total"] as? NSNumber)?.doubleValue ?? 1,
                     speed: ($0["speed_bps"] as? NSNumber)?.doubleValue ?? 0,
                     eta: ($0["eta_seconds"] as? NSNumber)?.doubleValue,
                     reconnects: ($0["reconnects"] as? NSNumber)?.intValue ?? 0)
        }
    }

    func save(_ changes: [String: Any]) async {
        var value = config
        for (key, change) in changes { value[key] = change }
        do {
            _ = try await call("api/config", body: value)
            notice = ""
        } catch {
            notice = error.localizedDescription
        }
        await refresh()
    }

    func setType(_ type: String, _ on: Bool) async {
        var value = types
        if on { value.insert(type) } else { value.remove(type) }
        guard !value.isEmpty else {
            notice = "至少要選一種裝置"
            return
        }
        await save(["types": ["iPhone", "iPad", "Mac"].filter(value.contains)])
    }

    func post(_ path: String, done: String) async {
        do {
            _ = try await call(path, body: [:])
            notice = done
        } catch {
            notice = error.localizedDescription
        }
    }

    func loadDevices() async {
        guard devices.isEmpty, let value = try? await call("api/devices") else { return }
        devices = (value["devices"] as? [[String: Any]] ?? []).compactMap {
            guard let id = $0["identifier"] as? String, let name = $0["name"] as? String else { return nil }
            return Device(id: id, name: name)
        }.reversed()
    }

    func saveMail(host: String, port: Int, username: String, to: String, password: String) async -> String? {
        // `security -i` reads the command from its input, so the password never appears in a process list.
        let quote = { (text: String) in "\"" + text.replacingOccurrences(of: "\\", with: "\\\\").replacingOccurrences(of: "\"", with: "\\\"") + "\"" }
        let command = "add-generic-password -U -s \(keychainService) -a \(quote(username)) -T /usr/bin/security -w \(quote(password))\n"
        let (code, output) = await runTool("/usr/bin/security", ["-i"], input: command)
        guard code == 0, !output.contains("SecKeychain") else { return "密碼無法存入鑰匙圈：" + output }
        let settings: [String: Any] = ["host": host, "port": port, "username": username, "to": to.isEmpty ? username : to]
        let file = supportFolder.appendingPathComponent("data/mail.json")
        do {
            try JSONSerialization.data(withJSONObject: settings).write(to: file, options: .atomic)
            try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: file.path)
        } catch {
            return "設定無法儲存：" + error.localizedDescription
        }
        mailReady = true
        return nil
    }

    func uninstall() async {
        timer?.invalidate()
        _ = await runTool("/bin/sh", [resource("mac/uninstall.sh")])
        NSApp.terminate(nil)
    }
}

let phaseNames = ["idle": "待命", "checking": "查詢中", "syncing": "下載中", "waiting": "等待重試"]
let transferNames = ["downloading": "下載中", "waiting_verify": "等待驗證", "verifying": "驗證中",
                     "waiting_export": "等待存入", "exporting": "存入 iTunes 資料夾", "reconnecting": "重新連線中"]

struct ContentView: View {
    @EnvironmentObject var model: Model

    var body: some View {
        Group {
            switch model.stage {
            case .starting:
                waiting("正在啟動…")
            case .installing:
                waiting("正在安裝背景服務…")
            case .needsTools:
                SetupView()
            case .failed(let reason):
                VStack(spacing: 14) {
                    Image(systemName: "exclamationmark.triangle").font(.system(size: 40)).foregroundStyle(.orange)
                    Text("安裝沒有完成").font(.title2.bold())
                    ScrollView { Text(reason).font(.callout).textSelection(.enabled).frame(maxWidth: .infinity, alignment: .leading) }
                        .frame(maxHeight: 160)
                    Button("再試一次") { Task { await model.install() } }.keyboardShortcut(.defaultAction)
                }.padding(40)
            case .ready:
                MainView()
            }
        }
        .frame(minWidth: 560, idealWidth: 600, minHeight: 640, idealHeight: 820)
        .task { await model.start() }
    }

    func waiting(_ text: String) -> some View {
        VStack(spacing: 14) { ProgressView(); Text(text).foregroundStyle(.secondary) }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
    }
}

struct SetupView: View {
    @EnvironmentObject var model: Model

    var body: some View {
        VStack(spacing: 16) {
            Image(systemName: "shippingbox").font(.system(size: 44)).foregroundStyle(.tint)
            Text("還差一個步驟").font(.title2.bold())
            Text("這台 Mac 需要先安裝 Apple 的「命令列開發者工具」（免費，約 5–10 分鐘）。\n\n按下面的按鈕後，會跳出 Apple 的安裝視窗，請按「安裝」並等它完成，再回來按「我裝好了」。")
                .multilineTextAlignment(.center).fixedSize(horizontal: false, vertical: true)
            HStack {
                Button("開始安裝工具") { Task { await model.requestTools() } }
                Button("我裝好了") { Task { await model.install() } }.keyboardShortcut(.defaultAction)
            }
        }.padding(44)
    }
}

struct Card<Content: View>: View {
    let title: String
    let symbol: String
    @ViewBuilder var content: Content

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Label(title, systemImage: symbol).font(.headline).foregroundStyle(.secondary)
            content
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(16)
        .background(.regularMaterial, in: RoundedRectangle(cornerRadius: 16, style: .continuous))
        .overlay(RoundedRectangle(cornerRadius: 16, style: .continuous).strokeBorder(.primary.opacity(0.08)))
    }
}

struct Tile: View {
    let label: String
    let value: String
    let detail: String
    var warning = false

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(label).font(.caption).foregroundStyle(.secondary)
            Text(value).font(.title3.weight(.semibold)).foregroundStyle(warning ? Color.orange : Color.primary)
                .lineLimit(1).minimumScaleFactor(0.7)
            Text(detail).font(.caption).foregroundStyle(.secondary).lineLimit(1).minimumScaleFactor(0.8)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(14)
        .background(.regularMaterial, in: RoundedRectangle(cornerRadius: 14, style: .continuous))
        .overlay(RoundedRectangle(cornerRadius: 14, style: .continuous).strokeBorder(.primary.opacity(0.08)))
    }
}

struct MainView: View {
    @EnvironmentObject var model: Model
    @State private var pickingModels = false
    @State private var settingMail = false
    @State private var confirmingRemoval = false

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 14) {
                hero
                if !model.online {
                    Label("暫時連不到背景服務，會自動重試。", systemImage: "wifi.exclamationmark").foregroundStyle(.orange)
                }
                if !model.notice.isEmpty {
                    Label(model.notice, systemImage: "info.circle.fill").foregroundStyle(.secondary)
                        .transition(.opacity)
                }
                HStack(spacing: 12) {
                    Tile(label: "最新版本", value: model.latest["iOS"] ?? model.latest.values.first ?? "—",
                         detail: Set(model.latest.values).count > 1 ? model.latest.sorted { $0.key < $1.key }.map { "\($0.key) \($0.value)" }.joined(separator: " · ") : "iOS · iPadOS · macOS")
                    Tile(label: "下載範圍", value: model.planCount > 0 ? "\(model.planCount) 個檔案" : "—", detail: model.planCount > 0 ? gigabytes(model.planBytes) : "尚未查詢")
                    Tile(label: "這台 Mac 可用空間", value: gigabytes(model.free), detail: model.planBytes > model.free ? "空間不夠放全部" : "空間足夠", warning: model.planBytes > model.free)
                }
                Card(title: "要下載哪些裝置", symbol: "square.stack.3d.up") { scope }
                Card(title: "目前進度", symbol: "arrow.down.circle") { progress }
                Card(title: "設定", symbol: "slider.horizontal.3") { settings }
                Card(title: "通知", symbol: "bell") { notifications }
                footer
            }
            .padding(20)
            .animation(.easeInOut(duration: 0.2), value: model.notice)
        }
        .background(LinearGradient(colors: [tone.opacity(0.14), .clear], startPoint: .top, endPoint: .center))
        .sheet(isPresented: $pickingModels) { ModelPicker(selected: Set(model.models)) }
        .sheet(isPresented: $settingMail) { MailSetup() }
        .confirmationDialog("確定要移除背景服務嗎？", isPresented: $confirmingRemoval) {
            Button("移除並結束", role: .destructive) { Task { await model.uninstall() } }
        } message: { Text("已下載的韌體、設定與紀錄都會保留。再打開這個 App 就會重新安裝。") }
    }

    var tone: Color { !model.enabled ? .gray : model.phase == "waiting" ? .orange : model.phase == "syncing" ? .blue : .green }
    var symbol: String { !model.enabled ? "pause.fill" : model.phase == "waiting" ? "exclamationmark" : model.phase == "syncing" ? "arrow.down" : "checkmark" }

    var hero: some View {
        HStack(spacing: 16) {
            ZStack {
                Circle().fill(.white.opacity(0.22)).frame(width: 52, height: 52)
                Image(systemName: symbol).font(.system(size: 22, weight: .bold)).foregroundStyle(.white)
            }
            VStack(alignment: .leading, spacing: 4) {
                Text(model.enabled ? (phaseNames[model.phase] ?? model.phase) : "已暫停").font(.title2.bold()).foregroundStyle(.white)
                Text(model.message).font(.callout).foregroundStyle(.white.opacity(0.88)).fixedSize(horizontal: false, vertical: true)
            }
            Spacer(minLength: 12)
            VStack(spacing: 6) {
                Toggle("自動下載", isOn: Binding(get: { model.enabled }, set: { value in Task { await model.save(["enabled": value]) } }))
                    .toggleStyle(.switch).controlSize(.large).labelsHidden().tint(.white.opacity(0.45))
                Text("自動下載").font(.caption.weight(.medium)).foregroundStyle(.white.opacity(0.9))
            }
        }
        .padding(20)
        .background(LinearGradient(colors: [tone, tone.opacity(0.72)], startPoint: .topLeading, endPoint: .bottomTrailing),
                    in: RoundedRectangle(cornerRadius: 20, style: .continuous))
        .shadow(color: tone.opacity(0.3), radius: 14, y: 6)
        .animation(.easeInOut(duration: 0.3), value: tone)
    }

    var scope: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(spacing: 10) {
                ForEach([("iPhone", "iphone"), ("iPad", "ipad"), ("Mac", "laptopcomputer")], id: \.0) { type, icon in
                    let on = model.types.contains(type)
                    Button { Task { await model.setType(type, !on) } } label: {
                        HStack(spacing: 8) {
                            Image(systemName: icon)
                            Text(type).fontWeight(.medium)
                            Spacer()
                            Image(systemName: on ? "checkmark.circle.fill" : "circle").foregroundStyle(on ? Color.white : Color.secondary.opacity(0.6))
                        }
                        .padding(.horizontal, 14).padding(.vertical, 11)
                        .foregroundStyle(on ? Color.white : Color.primary)
                        .background(on ? AnyShapeStyle(Color.accentColor) : AnyShapeStyle(Color.primary.opacity(0.06)), in: RoundedRectangle(cornerRadius: 12, style: .continuous))
                        .overlay(RoundedRectangle(cornerRadius: 12, style: .continuous).strokeBorder(.primary.opacity(on ? 0 : 0.12)))
                        .contentShape(Rectangle())
                    }.buttonStyle(.plain).accessibilityLabel(type).accessibilityValue(on ? "已選" : "未選")
                }
            }
            HStack {
                Text(model.models.isEmpty ? "所選裝置的全部機型" : "只下載指定的 \(model.models.count) 個機型").foregroundStyle(.secondary)
                Spacer()
                Button("選擇機型…") { pickingModels = true }
            }
        }
    }

    var progress: some View {
        VStack(alignment: .leading, spacing: 12) {
            if model.phase == "syncing", model.planCount > 0 {
                ProgressView(value: Double(model.doneCount), total: Double(model.planCount)) {
                    Text("已完成 \(model.doneCount) / \(model.planCount) 個檔案").font(.callout.weight(.medium))
                }
            }
            if model.transfers.isEmpty {
                HStack(spacing: 8) {
                    Image(systemName: model.phase == "syncing" ? "magnifyingglass" : "moon.zzz").foregroundStyle(.tertiary)
                    Text(model.phase == "syncing" ? "正在比對已有的檔案…" : "目前沒有正在下載的檔案").foregroundStyle(.secondary)
                }
            }
            ForEach(model.transfers) { item in
                VStack(alignment: .leading, spacing: 5) {
                    HStack {
                        Text(item.id).font(.callout.weight(.medium)).lineLimit(1).truncationMode(.middle)
                        Spacer()
                        Text("\(Int(min(item.progress / max(item.total, 1), 1) * 100))%").font(.callout.monospacedDigit()).foregroundStyle(.secondary)
                    }
                    ProgressView(value: min(item.progress, item.total), total: max(item.total, 1))
                        .tint(item.phase == "reconnecting" ? .orange : item.phase == "downloading" ? .blue : .green)
                    Text([transferNames[item.phase] ?? item.phase,
                          "\(gigabytes(item.progress)) / \(gigabytes(item.total))",
                          item.speed > 0 ? String(format: "%.1f MB/s", item.speed / 1e6) : "",
                          item.speed > 0 ? duration(item.eta) : "",
                          item.reconnects > 0 ? "已自動重連 \(item.reconnects) 次" : ""].filter { !$0.isEmpty }.joined(separator: " · "))
                        .font(.caption).foregroundStyle(.secondary)
                }
                .padding(10)
                .background(.primary.opacity(0.04), in: RoundedRectangle(cornerRadius: 10, style: .continuous))
            }
            Divider()
            HStack {
                Text("上次檢查 \(clock(model.lastCheck))　·　下次 \(clock(model.nextCheck))").font(.callout).foregroundStyle(.secondary)
                Spacer()
                Button { Task { await model.post("api/check", done: "已開始檢查") } } label: { Label("立即檢查", systemImage: "arrow.clockwise") }
            }
        }
    }

    var settings: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                Text("多久檢查一次新版本")
                Spacer()
                Picker("", selection: Binding(get: { model.interval }, set: { value in Task { await model.save(["interval_hours": value]) } })) {
                    ForEach([0.5, 1, 3, 6, 12, 24], id: \.self) { hours in
                        Text(hours < 1 ? "30 分鐘" : "\(Int(hours)) 小時").tag(hours)
                    }
                    if ![0.5, 1, 3, 6, 12, 24].contains(model.interval) { Text("\(model.interval, specifier: "%g") 小時").tag(model.interval) }
                }.labelsHidden().fixedSize()
            }
            Divider()
            HStack(alignment: .top) {
                VStack(alignment: .leading, spacing: 3) {
                    Text("偵測到新版時，先刪除舊版再下載")
                    Text("省空間。只會刪除這個 App 自己放進去的舊版檔案。").font(.caption).foregroundStyle(.secondary)
                }
                Spacer()
                Toggle("", isOn: Binding(get: { model.cleanupEarly }, set: { value in Task { await model.save(["cleanup_early": value, "cleanup": true]) } }))
                    .toggleStyle(.switch).labelsHidden()
            }
        }
    }

    var notifications: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(spacing: 8) {
                Image(systemName: model.mailReady ? "envelope.badge.fill" : "envelope").foregroundStyle(model.mailReady ? Color.green : Color.secondary)
                Text("偵測到新版本、以及全部下載完成時，會跳出系統通知" + (model.mailReady ? "並寄信。" : "。尚未設定寄信。"))
                    .fixedSize(horizontal: false, vertical: true)
            }
            if !model.notifyError.isEmpty { Text(model.notifyError).font(.callout).foregroundStyle(.orange) }
            HStack {
                Button(model.mailReady ? "修改寄信設定…" : "設定寄信…") { settingMail = true }
                Button("寄送測試通知") { Task { await model.post("api/notifications/test", done: "已送出測試通知，幾秒後會出現") } }
            }
        }
    }

    var footer: some View {
        HStack {
            Button { NSWorkspace.shared.open(firmwareFolder) } label: { Label("打開韌體資料夾", systemImage: "folder") }
                .controlSize(.large).buttonStyle(.borderedProminent)
            Menu("更多") {
                Button("進階設定（網頁）") { NSWorkspace.shared.open(serviceURL) }
                Button("移除背景服務…") { confirmingRemoval = true }
            }.controlSize(.large).fixedSize()
            Spacer()
            Label("關閉視窗後仍會在背景繼續下載", systemImage: "moon.stars").font(.caption).foregroundStyle(.secondary)
        }.padding(.top, 2)
    }
}

struct ModelPicker: View {
    @EnvironmentObject var model: Model
    @Environment(\.dismiss) var dismiss
    @State var selected: Set<String>
    @State private var search = ""

    var shown: [Device] {
        model.devices.filter { model.types.contains($0.kind) && (search.isEmpty || $0.name.localizedCaseInsensitiveContains(search) || $0.id.localizedCaseInsensitiveContains(search)) }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("選擇機型").font(.title3.bold())
            Text("都不勾＝下載所選裝置的全部機型。").foregroundStyle(.secondary)
            TextField("搜尋，例如 iPhone 16", text: $search).textFieldStyle(.roundedBorder)
            List(shown) { device in
                Toggle(isOn: Binding(get: { selected.contains(device.id) }, set: { value in
                    if value { selected.insert(device.id) } else { selected.remove(device.id) }
                })) {
                    HStack { Text(device.name); Spacer(); Text(device.id).foregroundStyle(.secondary).font(.caption) }
                }
            }.overlay { if model.devices.isEmpty { ProgressView() } }
            HStack {
                Button("全部取消勾選") { selected = [] }
                Spacer()
                Text(selected.isEmpty ? "全部機型" : "已選 \(selected.count) 個").foregroundStyle(.secondary)
                Button("取消") { dismiss() }.keyboardShortcut(.cancelAction)
                Button("儲存") {
                    Task {
                        await model.save(["models": model.devices.map(\.id).filter(selected.contains)])
                        dismiss()
                    }
                }.keyboardShortcut(.defaultAction)
            }
        }
        .padding(20).frame(width: 480, height: 560)
        .task { await model.loadDevices() }
    }
}

struct MailSetup: View {
    @EnvironmentObject var model: Model
    @Environment(\.dismiss) var dismiss
    @State private var host = "smtp.gmail.com"
    @State private var port = "587"
    @State private var username = ""
    @State private var to = ""
    @State private var password = ""
    @State private var error = ""
    @State private var saving = false

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("寄信設定").font(.title3.bold())
            Text("Gmail 請使用「應用程式密碼」，不是平常登入的密碼。密碼只會存進這台 Mac 的鑰匙圈。")
                .foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            Form {
                TextField("寄信伺服器", text: $host)
                TextField("連接埠", text: $port)
                TextField("寄件信箱", text: $username)
                SecureField("密碼", text: $password)
                TextField("收件信箱（空白＝寄給自己）", text: $to)
            }
            if !error.isEmpty { Text(error).foregroundStyle(.red).fixedSize(horizontal: false, vertical: true) }
            HStack {
                Spacer()
                Button("取消") { dismiss() }.keyboardShortcut(.cancelAction)
                Button("儲存") {
                    guard let number = Int(port), !host.isEmpty, username.contains("@"), !password.isEmpty else {
                        error = "請填寫伺服器、連接埠、寄件信箱和密碼"
                        return
                    }
                    saving = true
                    Task {
                        let problem = await model.saveMail(host: host, port: number, username: username, to: to, password: password)
                        saving = false
                        if let problem { error = problem } else {
                            model.notice = "寄信設定已儲存，可以按「寄送測試通知」確認"
                            dismiss()
                        }
                    }
                }.keyboardShortcut(.defaultAction).disabled(saving)
            }
        }.padding(20).frame(width: 440)
    }
}

final class AppDelegate: NSObject, NSApplicationDelegate {
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }
}

@main
struct AutoIPSWDownloaderApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) var delegate
    @StateObject private var model = Model()

    var body: some Scene {
        Window("Auto IPSW Downloader", id: "main") {
            ContentView().environmentObject(model)
        }
        .windowResizability(.contentMinSize)
        .defaultSize(width: 600, height: 820)
        .commands { CommandGroup(replacing: .newItem) {} }
    }
}
