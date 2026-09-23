// Draws the app icon — the brand mark from the UI (_ui.html brand_mark: a
// stone resting on the slope it keeps being pushed up) on a macOS-style rounded
// square — at every size an .icns needs, into an .iconset directory.
//
//   swift make_icon.swift <out.iconset>      then: iconutil -c icns <out.iconset>
//
// Drawn in code, not exported from a design tool, so the mark stays the
// same shapes as the one in the sidebar: the numbers below are its SVG's,
// on its 20-unit grid.
import AppKit

let out = URL(fileURLWithPath: CommandLine.arguments[1])
try? FileManager.default.createDirectory(at: out, withIntermediateDirectories: true)

func color(_ hex: UInt32, _ a: CGFloat = 1) -> NSColor {
    NSColor(srgbRed: CGFloat((hex >> 16) & 0xff) / 255, green: CGFloat((hex >> 8) & 0xff) / 255,
            blue: CGFloat(hex & 0xff) / 255, alpha: a)
}

func render(_ px: Int) -> Data {
    let rep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: px, pixelsHigh: px,
                               bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true, isPlanar: false,
                               colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0)!
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: rep)
    let s = CGFloat(px) / 1024                               // design on a 1024 canvas

    // The body: Apple's icon grid puts an 824-wide rounded square in 1024.
    let body = NSRect(x: 100 * s, y: 100 * s, width: 824 * s, height: 824 * s)
    let shape = NSBezierPath(roundedRect: body, xRadius: 186 * s, yRadius: 186 * s)
    NSGraphicsContext.saveGraphicsState()
    let shadow = NSShadow()
    shadow.shadowColor = color(0x000000, 0.35)
    shadow.shadowOffset = NSSize(width: 0, height: -10 * s)
    shadow.shadowBlurRadius = 24 * s
    shadow.set()
    color(0x0a0a0c).setFill()
    shape.fill()
    NSGraphicsContext.restoreGraphicsState()
    NSGradient(starting: color(0x1c1e23), ending: color(0x0a0a0c))!.draw(in: shape, angle: -90)
    color(0xffffff, 0.08).setStroke()
    shape.lineWidth = 3 * s
    shape.stroke()

    // The mark, from its 20-unit SVG grid (y flipped: AppKit's origin is bottom-left).
    let unit = 824 * s / 20 * 0.78
    let origin = NSPoint(x: body.midX - 10 * unit, y: body.midY - 10 * unit)
    func p(_ x: CGFloat, _ y: CGFloat) -> NSPoint { NSPoint(x: origin.x + x * unit, y: origin.y + (20 - y) * unit) }
    let amber = color(0xf5a623)

    let hill = NSBezierPath()
    hill.move(to: p(3, 17))
    hill.line(to: p(17, 5))
    hill.line(to: p(17, 17))
    hill.close()
    amber.withAlphaComponent(0.18).setFill()
    hill.fill()

    let slope = NSBezierPath()
    slope.move(to: p(3, 17))
    slope.line(to: p(17, 5))
    slope.lineWidth = 1.4 * unit
    slope.lineCapStyle = .round
    amber.withAlphaComponent(0.6).setStroke()
    slope.stroke()

    let c = p(6.9, 7.4), r = 3.2 * unit
    let stone = NSBezierPath(ovalIn: NSRect(x: c.x - r, y: c.y - r, width: 2 * r, height: 2 * r))
    stone.lineWidth = 1.6 * unit
    // a faint glow, so the stone reads at 16px as well as at 512
    NSGraphicsContext.saveGraphicsState()
    let glow = NSShadow()
    glow.shadowColor = amber.withAlphaComponent(0.45)
    glow.shadowBlurRadius = 18 * s
    glow.set()
    amber.setStroke()
    stone.stroke()
    NSGraphicsContext.restoreGraphicsState()

    NSGraphicsContext.restoreGraphicsState()
    return rep.representation(using: .png, properties: [:])!
}

for (name, px) in [("16x16", 16), ("16x16@2x", 32), ("32x32", 32), ("32x32@2x", 64),
                   ("128x128", 128), ("128x128@2x", 256), ("256x256", 256), ("256x256@2x", 512),
                   ("512x512", 512), ("512x512@2x", 1024)] {
    try render(px).write(to: out.appendingPathComponent("icon_\(name).png"))
}
