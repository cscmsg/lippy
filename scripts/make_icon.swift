// Generates Assets/AppIcon.icns. Run via `make icon`, or automatically from
// make_app.sh when the icns is missing.
//
// Artwork: the standard macOS icon grid (1024 canvas, 824pt squircle with 100pt
// margins), a warm coral gradient, and a cream speech bubble with a text caret
// inside it. Speech going in, text coming out.
//
// Deliberately NOT lips. "Lippy" already carries a lipstick sense in Australian
// and British English, and a pair of lips on the icon would settle that
// ambiguity the wrong way.
import CoreGraphics
import Foundation
import ImageIO
import UniformTypeIdentifiers

let sRGB = CGColorSpace(name: CGColorSpace.sRGB)!

func rgba(_ r: CGFloat, _ g: CGFloat, _ b: CGFloat, _ a: CGFloat = 1) -> CGColor {
    CGColor(colorSpace: sRGB, components: [r, g, b, a])!
}

func drawIcon(into ctx: CGContext, pixels: Int) {
    let f = CGFloat(pixels) / 1024.0
    ctx.scaleBy(x: f, y: f)

    let body = CGRect(x: 100, y: 100, width: 824, height: 824)
    let squircle = CGPath(roundedRect: body, cornerWidth: 185, cornerHeight: 185, transform: nil)

    ctx.saveGState()
    ctx.setShadow(offset: CGSize(width: 0, height: -14), blur: 36,
                  color: CGColor(gray: 0, alpha: 0.35))
    ctx.addPath(squircle)
    ctx.setFillColor(rgba(0.82, 0.42, 0.28))
    ctx.fillPath()
    ctx.restoreGState()

    ctx.saveGState()
    ctx.addPath(squircle)
    ctx.clip()
    let warm = CGGradient(colorsSpace: sRGB,
                          colors: [rgba(0.91, 0.49, 0.31), rgba(0.75, 0.32, 0.22)] as CFArray,
                          locations: [0, 1])!
    ctx.drawLinearGradient(warm, start: CGPoint(x: 512, y: 924),
                           end: CGPoint(x: 512, y: 100), options: [])
    ctx.restoreGState()

    // Speech bubble: rounded rect plus tail, filled as one path so the join
    // stays clean at 16px where the tail is barely two pixels wide.
    let cream = rgba(0.980, 0.973, 0.957)
    let bubble = CGRect(x: 236, y: 330, width: 552, height: 380)
    let bubblePath = CGMutablePath()
    bubblePath.addRoundedRect(in: bubble, cornerWidth: 96, cornerHeight: 96)

    let tail = CGMutablePath()
    tail.move(to: CGPoint(x: 366, y: 372))
    tail.addLine(to: CGPoint(x: 322, y: 214))
    tail.addLine(to: CGPoint(x: 486, y: 356))
    tail.closeSubpath()

    ctx.saveGState()
    ctx.setShadow(offset: CGSize(width: 0, height: -8), blur: 18,
                  color: CGColor(gray: 0, alpha: 0.22))
    ctx.addPath(bubblePath)
    ctx.addPath(tail)
    ctx.setFillColor(cream)
    ctx.fillPath()
    ctx.restoreGState()

    // A text caret inside the bubble: the cursor the words land on.
    let ink = rgba(0.62, 0.24, 0.16)
    ctx.addPath(CGPath(roundedRect: CGRect(x: 487, y: 418, width: 50, height: 204),
                       cornerWidth: 25, cornerHeight: 25, transform: nil))
    ctx.setFillColor(ink)
    ctx.fillPath()

    // Serifs top and bottom so it reads as a text cursor, not a bar.
    for y in [CGFloat(400), CGFloat(602)] {
        ctx.addPath(CGPath(roundedRect: CGRect(x: 427, y: y, width: 170, height: 38),
                           cornerWidth: 19, cornerHeight: 19, transform: nil))
        ctx.setFillColor(ink)
        ctx.fillPath()
    }
}

func writePNG(pixels: Int, to url: URL) {
    guard let ctx = CGContext(data: nil, width: pixels, height: pixels,
                              bitsPerComponent: 8, bytesPerRow: 0, space: sRGB,
                              bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue) else {
        fatalError("could not create a \(pixels)px context")
    }
    ctx.interpolationQuality = .high
    drawIcon(into: ctx, pixels: pixels)
    guard let image = ctx.makeImage(),
          let dest = CGImageDestinationCreateWithURL(
              url as CFURL, UTType.png.identifier as CFString, 1, nil) else {
        fatalError("could not encode \(url.lastPathComponent)")
    }
    CGImageDestinationAddImage(dest, image, nil)
    CGImageDestinationFinalize(dest)
}

let root = URL(fileURLWithPath: FileManager.default.currentDirectoryPath)
let iconset = root.appendingPathComponent("Assets/AppIcon.iconset")
try? FileManager.default.createDirectory(at: iconset, withIntermediateDirectories: true)

let variants: [(String, Int)] = [
    ("icon_16x16", 16), ("icon_16x16@2x", 32),
    ("icon_32x32", 32), ("icon_32x32@2x", 64),
    ("icon_128x128", 128), ("icon_128x128@2x", 256),
    ("icon_256x256", 256), ("icon_256x256@2x", 512),
    ("icon_512x512", 512), ("icon_512x512@2x", 1024),
]
for (name, px) in variants {
    writePNG(pixels: px, to: iconset.appendingPathComponent("\(name).png"))
}
print("wrote \(variants.count) sizes to Assets/AppIcon.iconset")
