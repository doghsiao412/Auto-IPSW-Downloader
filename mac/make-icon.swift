// Draws the app icon. Usage: swift mac/make-icon.swift <output.png>
import AppKit

let size = 1024.0
let image = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: Int(size), pixelsHigh: Int(size), bitsPerSample: 8,
                             samplesPerPixel: 4, hasAlpha: true, isPlanar: false, colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0)!
NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: image)
let context = NSGraphicsContext.current!.cgContext

// macOS icon grid: an 824pt rounded square centred on a 1024pt canvas, with a soft drop shadow.
let plate = CGRect(x: 100, y: 100, width: 824, height: 824)
let shape = CGPath(roundedRect: plate, cornerWidth: 186, cornerHeight: 186, transform: nil)
context.saveGState()
context.setShadow(offset: CGSize(width: 0, height: -12), blur: 28, color: CGColor(gray: 0, alpha: 0.35))
context.addPath(shape)
context.setFillColor(CGColor(red: 0.1, green: 0.3, blue: 0.9, alpha: 1))
context.fillPath()
context.restoreGState()
context.saveGState()
context.addPath(shape)
context.clip()
let colors = [CGColor(red: 0.20, green: 0.62, blue: 1.0, alpha: 1), CGColor(red: 0.22, green: 0.28, blue: 0.86, alpha: 1)] as CFArray
let gradient = CGGradient(colorsSpace: CGColorSpaceCreateDeviceRGB(), colors: colors, locations: [0, 1])!
context.drawLinearGradient(gradient, start: CGPoint(x: 512, y: 924), end: CGPoint(x: 512, y: 100), options: [])
// A gentle highlight across the top half.
let glow = CGGradient(colorsSpace: CGColorSpaceCreateDeviceRGB(), colors: [CGColor(gray: 1, alpha: 0.22), CGColor(gray: 1, alpha: 0)] as CFArray, locations: [0, 1])!
context.drawRadialGradient(glow, startCenter: CGPoint(x: 360, y: 900), startRadius: 0, endCenter: CGPoint(x: 360, y: 900), endRadius: 620, options: [])
context.restoreGState()

// Download arrow into a tray, the same mark as the web page.
context.setStrokeColor(CGColor(gray: 1, alpha: 1))
context.setLineWidth(64)
context.setLineCap(.round)
context.setLineJoin(.round)
context.setShadow(offset: CGSize(width: 0, height: -8), blur: 18, color: CGColor(red: 0.05, green: 0.1, blue: 0.4, alpha: 0.35))
context.move(to: CGPoint(x: 512, y: 760)); context.addLine(to: CGPoint(x: 512, y: 452))
context.move(to: CGPoint(x: 384, y: 572)); context.addLine(to: CGPoint(x: 512, y: 444)); context.addLine(to: CGPoint(x: 640, y: 572))
context.move(to: CGPoint(x: 306, y: 372)); context.addLine(to: CGPoint(x: 306, y: 286)); context.addLine(to: CGPoint(x: 718, y: 286)); context.addLine(to: CGPoint(x: 718, y: 372))
context.strokePath()
try! image.representation(using: .png, properties: [:])!.write(to: URL(fileURLWithPath: CommandLine.arguments[1]))
