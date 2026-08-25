// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "Lippy",
    platforms: [.macOS(.v14)],
    targets: [
        .executableTarget(name: "Lippy")
    ]
)
