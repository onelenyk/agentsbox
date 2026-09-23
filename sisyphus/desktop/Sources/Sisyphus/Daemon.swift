import Foundation

/// The daemon as a child of this app: started with it, stopped with it.
///
/// The app is the daemon's only owner. It passes its own pid as
/// SISYPHUS_PARENT_PID, and the daemon leaves by itself when that pid is gone
/// (core/lifecycle.py) — so even a crashed or force-quit app does not leave a
/// daemon behind on the port.
final class Daemon {
    let root: URL            // .../sisyphus — the daemon's working directory
    let python: String
    /// 7677 unless SISYPHUS_PORT says otherwise (a second copy, for testing).
    let port = Int(ProcessInfo.processInfo.environment["SISYPHUS_PORT"] ?? "") ?? 7677
    private(set) var process: Process?

    var base: URL { URL(string: "http://127.0.0.1:\(port)")! }
    var logURL: URL { root.appendingPathComponent("logs/daemon.app.log") }

    init(root: URL, python: String) {
        self.root = root
        self.python = python
    }

    // MARK: - What else is out there

    /// The pid listening on our port, if any.
    func portOwner() -> Int32? {
        let out = Shell.run("/usr/sbin/lsof", ["-nP", "-iTCP:\(port)", "-sTCP:LISTEN", "-t"])
        return out.split(separator: "\n").compactMap { Int32($0.trimmingCharacters(in: .whitespaces)) }.first
    }

    /// The launchd agent that used to own the daemon (`sisyphus daemon install`).
    /// While it is loaded it restarts any daemon we stop, so the app turns it off.
    var launchdAgentLoaded: Bool {
        port == 7677 && Shell.status("/bin/launchctl", ["list", "com.sisyphus.daemon"]) == 0
    }

    func uninstallLaunchdAgent() {
        _ = Shell.run(python, ["sisyphus.py", "daemon", "uninstall"], cwd: root)
    }

    /// Ask whatever daemon holds the port to stop the way we stop ours; kill
    /// it if it will not. Returns when the port is free (or gives up).
    func stopForeign(pid: Int32) async {
        await requestShutdown()
        if await waitPortFree(seconds: 25) { return }
        kill(pid, SIGTERM)
        if await waitPortFree(seconds: 5) { return }
        kill(pid, SIGKILL)
        _ = await waitPortFree(seconds: 3)
    }

    // MARK: - Our own

    func start() throws {
        try FileManager.default.createDirectory(at: logURL.deletingLastPathComponent(),
                                                withIntermediateDirectories: true)
        if !FileManager.default.fileExists(atPath: logURL.path) {
            FileManager.default.createFile(atPath: logURL.path, contents: nil)
        }
        let log = try FileHandle(forWritingTo: logURL)
        log.seekToEndOfFile()

        let p = Process()
        p.executableURL = URL(fileURLWithPath: python)
        p.arguments = ["-m", "uvicorn", "web.app:app",
                       "--host", "127.0.0.1", "--port", String(port),
                       "--timeout-graceful-shutdown", "5"]
        p.currentDirectoryURL = root
        var env = ProcessInfo.processInfo.environment
        env["SISYPHUS_PARENT_PID"] = String(getpid())
        env["PATH"] = Shell.loginPath() ?? env["PATH"]
        p.environment = env
        p.standardOutput = log
        p.standardError = log
        try p.run()
        process = p
    }

    var isRunning: Bool { process?.isRunning ?? false }

    /// Poll health until the daemon answers ready, or give up.
    func waitReady(seconds: Double) async -> Bool {
        let deadline = Date().addingTimeInterval(seconds)
        while Date() < deadline {
            if let h = await health(), h["ok"] as? Bool == true { return true }
            if process != nil && !isRunning { return false }     // it died on the way up
            try? await Task.sleep(for: .milliseconds(250))
        }
        return false
    }

    func health() async -> [String: Any]? {
        var req = URLRequest(url: base.appendingPathComponent("api/v1/health"))
        req.timeoutInterval = 2
        guard let (data, _) = try? await URLSession.shared.data(for: req),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return nil }
        return obj
    }

    /// Worker runs in flight — what quitting would stop.
    func runningWorkers() async -> [[String: Any]] {
        (await health())?["workers"] as? [[String: Any]] ?? []
    }

    func requestShutdown() async {
        var req = URLRequest(url: base.appendingPathComponent("api/v1/shutdown"))
        req.httpMethod = "POST"
        req.timeoutInterval = 3
        _ = try? await URLSession.shared.data(for: req)
    }

    /// Stop our daemon: ask, wait for it to park its work and leave, then
    /// insist. The daemon gives workers 15s, so the polite wait is longer.
    func stop() async {
        guard let p = process, p.isRunning else { return }
        await requestShutdown()
        if await waitExit(p, seconds: 25) { return }
        p.terminate()                                    // SIGTERM
        if await waitExit(p, seconds: 5) { return }
        kill(p.processIdentifier, SIGKILL)
        _ = await waitExit(p, seconds: 2)
    }

    func logTail(lines: Int = 30) -> String {
        guard let text = try? String(contentsOf: logURL, encoding: .utf8) else { return "" }
        return text.split(separator: "\n", omittingEmptySubsequences: false).suffix(lines).joined(separator: "\n")
    }

    // MARK: - Waiting

    private func waitExit(_ p: Process, seconds: Double) async -> Bool {
        let deadline = Date().addingTimeInterval(seconds)
        while p.isRunning && Date() < deadline { try? await Task.sleep(for: .milliseconds(150)) }
        return !p.isRunning
    }

    private func waitPortFree(seconds: Double) async -> Bool {
        let deadline = Date().addingTimeInterval(seconds)
        while Date() < deadline {
            if portOwner() == nil { return true }
            try? await Task.sleep(for: .milliseconds(250))
        }
        return portOwner() == nil
    }
}

/// Small, synchronous process helpers for one-off commands.
enum Shell {
    @discardableResult
    static func run(_ exe: String, _ args: [String], cwd: URL? = nil) -> String {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: exe)
        p.arguments = args
        if let cwd { p.currentDirectoryURL = cwd }
        let pipe = Pipe()
        p.standardOutput = pipe
        p.standardError = Pipe()
        guard (try? p.run()) != nil else { return "" }
        let data = pipe.fileHandleForReading.readDataToEndOfFile()
        p.waitUntilExit()
        return String(data: data, encoding: .utf8) ?? ""
    }

    static func status(_ exe: String, _ args: [String]) -> Int32 {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: exe)
        p.arguments = args
        p.standardOutput = Pipe()
        p.standardError = Pipe()
        guard (try? p.run()) != nil else { return -1 }
        p.waitUntilExit()
        return p.terminationStatus
    }

    /// The PATH a login shell would have. An app started from Finder gets only
    /// the system's, and the daemon needs git and the runner CLIs on it.
    static func loginPath() -> String? {
        let shell = ProcessInfo.processInfo.environment["SHELL"] ?? "/bin/zsh"
        let out = run(shell, ["-lc", "printf %s \"$PATH\""]).trimmingCharacters(in: .whitespacesAndNewlines)
        return out.isEmpty ? nil : out
    }
}
