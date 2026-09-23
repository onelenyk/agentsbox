import AppKit
import WebKit

/// Open the app → the daemon starts and the UI loads. Quit → the daemon
/// parks its work and stops, then the app goes. One window; closing it quits.
final class AppDelegate: NSObject, NSApplicationDelegate {
    private var window: NSWindow!
    private var web: WebView!
    private var daemon: Daemon!
    private var quitting = false

    func applicationDidFinishLaunching(_ note: Notification) {
        daemon = Daemon(root: Config.root, python: Config.python)
        Menus.install()
        buildWindow()
        Task { await boot() }
    }

    // MARK: - Window

    private func buildWindow() {
        web = WebView()
        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1320, height: 860),
                          styleMask: [.titled, .closable, .miniaturizable, .resizable],
                          backing: .buffered, defer: false)
        // A thin bar of the page's own colour above the content: the traffic
        // lights keep clear of the sidebar, and the window has a place to be
        // dragged by (a web view under the title bar would swallow the drag).
        window.title = "Sisyphus"
        window.titleVisibility = .hidden
        window.titlebarAppearsTransparent = true
        window.backgroundColor = NSColor(red: 0.039, green: 0.039, blue: 0.047, alpha: 1)
        window.isReleasedWhenClosed = false                 // ⌘W then quit still has a window to show "Stopping…" in
        window.minSize = NSSize(width: 720, height: 480)
        window.contentView = web.view
        window.center()
        window.setFrameAutosaveName("SisyphusMain")       // remembers size and place
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    // MARK: - Boot

    @MainActor
    private func boot() async {
        web.showStatus("Starting Sisyphus…")

        if daemon.launchdAgentLoaded {
            let ok = ask("Sisyphus is set to run in the background",
                         "A login item keeps the daemon running when this app is closed, and restarts it when the app stops it. The app runs the daemon now — turn the login item off?",
                         yes: "Turn it off", no: "Quit")
            guard ok else { NSApp.terminate(nil); return }
            web.showStatus("Turning off the background login item…")
            await Task.detached { [daemon] in daemon!.uninstallLaunchdAgent() }.value
        }

        if let pid = daemon.portOwner() {
            let ok = ask("Another Sisyphus is already running",
                         "Process \(pid) holds port \(daemon.port). The app starts and stops its own daemon — stop that one? Its running work is parked, and a reply resumes it.",
                         yes: "Stop it", no: "Quit")
            guard ok else { NSApp.terminate(nil); return }
            web.showStatus("Stopping the other Sisyphus…")
            await daemon.stopForeign(pid: pid)
        }

        do {
            try daemon.start()
        } catch {
            web.showStatus("Could not start the daemon", detail: "\(error)")
            return
        }
        if await daemon.waitReady(seconds: 45) {
            web.load(daemon.base.appendingPathComponent("dashboard"))
        } else {
            web.showStatus("Sisyphus did not start", detail: daemon.logTail())
        }
    }

    // MARK: - Quit

    func applicationShouldTerminateAfterLastWindowClosed(_ app: NSApplication) -> Bool { true }

    func applicationShouldTerminate(_ app: NSApplication) -> NSApplication.TerminateReply {
        guard daemon?.isRunning == true else { return .terminateNow }
        if quitting { return .terminateLater }
        Task { @MainActor in
            let workers = await daemon.runningWorkers()
            if !workers.isEmpty {
                let names = workers.map { w in
                    "\(w["role"] as? String ?? "worker") on \(w["task_id"] as? String ?? "?")"
                }.joined(separator: "\n")
                let ok = ask("\(workers.count) agent\(workers.count == 1 ? " is" : "s are") working",
                             "Quitting stops them. Their tasks park as waiting, and a reply resumes them.\n\n\(names)",
                             yes: "Quit and stop them", no: "Keep working")
                if !ok {
                    app.reply(toApplicationShouldTerminate: false)
                    return
                }
            }
            quitting = true
            window.makeKeyAndOrderFront(nil)
            web.showStatus("Stopping Sisyphus…", detail: workers.isEmpty ? "" : "Parking the work in flight.")
            await daemon.stop()
            app.reply(toApplicationShouldTerminate: true)
        }
        return .terminateLater
    }

    // MARK: -

    @MainActor
    private func ask(_ title: String, _ text: String, yes: String, no: String) -> Bool {
        let a = NSAlert()
        a.messageText = title
        a.informativeText = text
        a.addButton(withTitle: yes)
        a.addButton(withTitle: no)
        return a.runModal() == .alertFirstButtonReturn
    }
}

/// Where the daemon lives and what runs it. Baked into Info.plist by build.sh;
/// SISYPHUS_ROOT / SISYPHUS_PYTHON in the environment override for development.
enum Config {
    static var root: URL {
        let env = ProcessInfo.processInfo.environment["SISYPHUS_ROOT"]
        let plist = Bundle.main.object(forInfoDictionaryKey: "SisyphusRoot") as? String
        return URL(fileURLWithPath: env ?? plist ?? FileManager.default.currentDirectoryPath)
    }

    static var python: String {
        ProcessInfo.processInfo.environment["SISYPHUS_PYTHON"]
            ?? Bundle.main.object(forInfoDictionaryKey: "SisyphusPython") as? String
            ?? "/opt/homebrew/bin/python3"
    }
}
