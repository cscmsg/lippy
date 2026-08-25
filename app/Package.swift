// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "LocalFlow",
    platforms: [.macOS(.v14)],
    targets: [
        .executableTarget(name: "LocalFlow")
    ]
)
