// swift-tools-version:5.9
// Sisyphus.app — a native window over the daemon, and the daemon's owner:
// open the app and the work runs; quit it and the work stops.
// Build the .app with ./build.sh (swift build + an app bundle around it).
import PackageDescription

let package = Package(
    name: "Sisyphus",
    platforms: [.macOS(.v14)],
    targets: [
        .executableTarget(name: "Sisyphus", path: "Sources/Sisyphus"),
    ]
)
