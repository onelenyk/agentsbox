import AppKit
import WebKit

/// The main menu. Edit is not decoration: without Cut/Copy/Paste items the
/// web view gets no ⌘C/⌘V at all.
enum Menus {
    static func install() {
        let main = NSMenu()

        main.addItem(submenu("Sisyphus", [
            item("About Sisyphus", #selector(NSApplication.orderFrontStandardAboutPanel(_:))),
            .separator(),
            item("Hide Sisyphus", #selector(NSApplication.hide(_:)), "h"),
            item("Hide Others", #selector(NSApplication.hideOtherApplications(_:)), "h", [.command, .option]),
            item("Show All", #selector(NSApplication.unhideAllApplications(_:))),
            .separator(),
            item("Quit Sisyphus", #selector(NSApplication.terminate(_:)), "q"),
        ]))

        main.addItem(submenu("Edit", [
            item("Undo", Selector(("undo:")), "z"),
            item("Redo", Selector(("redo:")), "z", [.command, .shift]),
            .separator(),
            item("Cut", #selector(NSText.cut(_:)), "x"),
            item("Copy", #selector(NSText.copy(_:)), "c"),
            item("Paste", #selector(NSText.paste(_:)), "v"),
            item("Select All", #selector(NSText.selectAll(_:)), "a"),
        ]))

        main.addItem(submenu("View", [
            item("Reload", #selector(WKWebView.reload(_:)), "r"),
            item("Back", #selector(WKWebView.goBack(_:)), "["),
            item("Forward", #selector(WKWebView.goForward(_:)), "]"),
            .separator(),
            item("Enter Full Screen", #selector(NSWindow.toggleFullScreen(_:)), "f", [.command, .control]),
        ]))

        let window = submenu("Window", [
            item("Minimize", #selector(NSWindow.performMiniaturize(_:)), "m"),
            item("Zoom", #selector(NSWindow.performZoom(_:))),
            .separator(),
            item("Close", #selector(NSWindow.performClose(_:)), "w"),
        ])
        main.addItem(window)
        NSApp.windowsMenu = window.submenu

        NSApp.mainMenu = main
    }

    private static func submenu(_ title: String, _ items: [NSMenuItem]) -> NSMenuItem {
        let holder = NSMenuItem()
        let menu = NSMenu(title: title)
        items.forEach(menu.addItem)
        holder.submenu = menu
        return holder
    }

    private static func item(_ title: String, _ action: Selector, _ key: String = "",
                             _ mods: NSEvent.ModifierFlags = [.command]) -> NSMenuItem {
        let i = NSMenuItem(title: title, action: action, keyEquivalent: key)
        i.keyEquivalentModifierMask = mods
        return i
    }
}
