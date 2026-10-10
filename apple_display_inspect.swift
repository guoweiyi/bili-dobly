// Read-only model and display/EDR diagnostics. Never opens a window or changes
// brightness, profiles, playback, or routing; no device IDs or serials emitted.
// Build: ./scripts/build_apple_tools.sh
// Run: ./build/apple_display_inspect
import Foundation
import AppKit
import AVFoundation
import CoreGraphics
import IOKit.ps

if Array(CommandLine.arguments.dropFirst()) == ["--help"] {
    print("Usage: apple_display_inspect\nRead-only screen/EDR diagnostics as JSON. No window, playback, settings changes, display IDs, or serial numbers.")
    exit(0)
}
guard CommandLine.arguments.count == 1 else {
    FileHandle.standardError.write(Data("Usage: apple_display_inspect [--help]\n".utf8))
    exit(64)
}

func modelIdentifier() -> String? {
    var size = 0
    guard sysctlbyname("hw.model", nil, &size, nil, 0) == 0, size > 0 else { return nil }
    var bytes = [CChar](repeating: 0, count: size)
    guard sysctlbyname("hw.model", &bytes, &size, nil, 0) == 0 else { return nil }
    return String(cString: bytes)
}

func currentPowerSource() -> String? {
    guard let information = IOPSCopyPowerSourcesInfo()?.takeRetainedValue(),
          let source = IOPSGetProvidingPowerSourceType(information)?.takeUnretainedValue() else { return nil }
    return source as String
}

let screens: [[String: Any]] = NSScreen.screens.enumerated().map { index, screen in
    var result: [String: Any] = [
        "screenIndex": index,
        "framePoints": ["width": screen.frame.width, "height": screen.frame.height],
        "backingScaleFactor": screen.backingScaleFactor,
        "supportsDisplayP3": screen.canRepresent(.p3),
        "currentEDRHeadroom": screen.maximumExtendedDynamicRangeColorComponentValue,
        "potentialEDRHeadroom": screen.maximumPotentialExtendedDynamicRangeColorComponentValue,
        "referenceEDRHeadroom": screen.maximumReferenceExtendedDynamicRangeColorComponentValue,
        "supportsEDR": screen.maximumPotentialExtendedDynamicRangeColorComponentValue > 1,
        "supportsReferenceRendering": screen.maximumReferenceExtendedDynamicRangeColorComponentValue > 0
    ]
    if let number = screen.deviceDescription[NSDeviceDescriptionKey("NSScreenNumber")] as? NSNumber {
        let display = CGDirectDisplayID(number.uint32Value)
        result["isBuiltIn"] = CGDisplayIsBuiltin(display) != 0
        result["cgDisplayDimensions"] = ["width": CGDisplayPixelsWide(display), "height": CGDisplayPixelsHigh(display)]
    }
    return result
}
let report: [String: Any] = [
    "schemaVersion": 1,
    "operation": "read_only_display_edr_inspection",
    "checkedAtUTC": ISO8601DateFormatter().string(from: Date()),
    "modelIdentifier": modelIdentifier() as Any? ?? NSNull(),
    "osVersion": ProcessInfo.processInfo.operatingSystemVersionString,
    "avPlayerEligibleForHDRPlayback": AVPlayer.eligibleForHDRPlayback,
    "powerSource": currentPowerSource() as Any? ?? NSNull(),
    "screens": screens,
    "note": "Current EDR headroom is dynamic and system-wide. It does not prove a particular player is applying Dolby Vision RPU metadata."
]
do {
    let data = try JSONSerialization.data(withJSONObject: report, options: [.prettyPrinted, .sortedKeys])
    FileHandle.standardOutput.write(data)
    FileHandle.standardOutput.write(Data([10]))
} catch {
    FileHandle.standardError.write(Data("Display inspection could not serialize its report: \(error.localizedDescription)\n".utf8))
    exit(1)
}
