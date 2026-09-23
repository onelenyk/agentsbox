import AppKit
import WebKit

/// The UI, in the system's own WebKit — the same engine as Safari, so no
/// bundled browser and no second copy of the interface. Everything the daemon
/// serves renders here; links that leave it open in the default browser.
final class WebView: NSObject, WKNavigationDelegate, WKUIDelegate {
    let view: WKWebView

    override init() {
        let config = WKWebViewConfiguration()
        config.websiteDataStore = .default()          // keeps theme and filters between launches
        view = WKWebView(frame: .zero, configuration: config)
        super.init()
        view.navigationDelegate = self
        view.uiDelegate = self
        view.isInspectable = true                     // Safari → Develop, for debugging the UI
        view.allowsBackForwardNavigationGestures = true
        view.setValue(false, forKey: "drawsBackground")   // no white flash before the first paint
    }

    func load(_ url: URL) {
        view.load(URLRequest(url: url))
    }

    /// A plain status page while the daemon starts or stops.
    func showStatus(_ title: String, detail: String = "") {
        let esc = { (s: String) in
            s.replacingOccurrences(of: "&", with: "&amp;").replacingOccurrences(of: "<", with: "&lt;")
        }
        let html = """
        <!doctype html><meta charset="utf-8">
        <style>
          html,body{margin:0;height:100%;background:#0a0a0c;color:#ece9e3;
            font:14px -apple-system,BlinkMacSystemFont,sans-serif;-webkit-font-smoothing:antialiased}
          main{height:100%;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:14px}
          h1{font-size:15px;font-weight:600;margin:0;letter-spacing:.01em}
          pre{max-width:80%;max-height:50%;overflow:auto;margin:0;padding:12px 14px;border-radius:9px;
            background:#111216;border:1px solid #23252b;color:#b3b0a8;font:11.5px ui-monospace,Menlo,monospace;white-space:pre-wrap}
          svg{color:#f5a623}
        </style>
        <main>
          <svg width="34" height="34" viewBox="0 0 20 20" fill="none"><path d="M3 16.5 15.5 4" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" opacity=".55"/><circle cx="8.5" cy="11" r="3.2" stroke="currentColor" stroke-width="1.6"/></svg>
          <h1>\(esc(title))</h1>
          \(detail.isEmpty ? "" : "<pre>\(esc(detail))</pre>")
        </main>
        """
        view.loadHTMLString(html, baseURL: nil)
    }

    // MARK: - Links that leave the app

    private func isLocal(_ url: URL) -> Bool {
        guard let host = url.host else { return true }       // about:, data:
        return host == "127.0.0.1" || host == "localhost"
    }

    func webView(_ webView: WKWebView, decidePolicyFor action: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        if let url = action.request.url, ["http", "https"].contains(url.scheme ?? ""), !isLocal(url) {
            NSWorkspace.shared.open(url)
            decisionHandler(.cancel)
            return
        }
        decisionHandler(.allow)
    }

    /// target="_blank" and window.open: local ones load here, the rest go out.
    func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration,
                 for action: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? {
        if let url = action.request.url {
            if isLocal(url) { webView.load(URLRequest(url: url)) } else { NSWorkspace.shared.open(url) }
        }
        return nil
    }

    // MARK: - The browser's dialogs, as the system's
    // htmx's hx-confirm is window.confirm — without these, every "Delete…?"
    // would silently answer no.

    func webView(_ webView: WKWebView, runJavaScriptAlertPanelWithMessage message: String,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping () -> Void) {
        let a = NSAlert()
        a.messageText = message
        a.runModal()
        completionHandler()
    }

    func webView(_ webView: WKWebView, runJavaScriptConfirmPanelWithMessage message: String,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping (Bool) -> Void) {
        let a = NSAlert()
        a.messageText = message
        a.addButton(withTitle: "OK")
        a.addButton(withTitle: "Cancel")
        completionHandler(a.runModal() == .alertFirstButtonReturn)
    }

    func webView(_ webView: WKWebView, runJavaScriptTextInputPanelWithPrompt prompt: String,
                 defaultText: String?, initiatedByFrame frame: WKFrameInfo,
                 completionHandler: @escaping (String?) -> Void) {
        let a = NSAlert()
        a.messageText = prompt
        let field = NSTextField(frame: NSRect(x: 0, y: 0, width: 280, height: 24))
        field.stringValue = defaultText ?? ""
        a.accessoryView = field
        a.addButton(withTitle: "OK")
        a.addButton(withTitle: "Cancel")
        completionHandler(a.runModal() == .alertFirstButtonReturn ? field.stringValue : nil)
    }

    /// <input type="file"> — the task box's "attach a file".
    func webView(_ webView: WKWebView, runOpenPanelWith parameters: WKOpenPanelParameters,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping ([URL]?) -> Void) {
        let panel = NSOpenPanel()
        panel.allowsMultipleSelection = parameters.allowsMultipleSelection
        panel.canChooseDirectories = parameters.allowsDirectories
        completionHandler(panel.runModal() == .OK ? panel.urls : nil)
    }
}
