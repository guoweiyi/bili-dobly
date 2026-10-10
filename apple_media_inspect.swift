// Inspect a local MP4 through Apple's media framework, without playing it.
// Build: ./scripts/build_apple_tools.sh
// Run:   ./build/apple_media_inspect /path/to/movie.mp4
//
// Dolby recommends the richest declared CoreMedia format ID ec+3 (0x65632B33)
// to identify DD+ JOC, and GetRichestDecodableFormat to test decoder support:
// https://professionalsupport.dolby.com/s/article/Editing-Dolby-Digital-Plus-with-Dolby-Atmos
// Apple documents hvc1 / Main10 / dvvC / BT.2020 / HLG for DV Profile 8.4:
// https://developer.apple.com/documentation/technotes/tn3145-hdr-video-metadata
// Dolby's local playback guide uses QuickLook/QuickTime/Safari on Mac and Files
// on iOS, with compatible headphones and Control Center Fixed/Head Tracked:
// https://professionalsupport.dolby.com/s/article/How-can-I-QC-my-mix-on-Apple-devices
// This report confirms recognition and decoder availability. It does not prove
// that a particular audio output route is currently rendering Spatial Audio.

import Foundation
import AVFoundation
import CoreMedia
import AudioToolbox

private let ec3JOC: AudioFormatID = 0x65632B33

private func fourCC(_ value: UInt32) -> String {
    let bytes: [UInt8] = [UInt8((value >> 24) & 255), UInt8((value >> 16) & 255),
                         UInt8((value >> 8) & 255), UInt8(value & 255)]
    return String(bytes: bytes, encoding: .ascii) ?? String(format: "0x%08X", value)
}

private func jsonValue(_ value: Any) -> Any {
    if let data = value as? Data {
        return ["bytes": data.count, "hex": data.map { String(format: "%02x", $0) }.joined()]
    }
    if let dictionary = value as? NSDictionary {
        var result: [String: Any] = [:]
        for (key, item) in dictionary { result[String(describing: key)] = jsonValue(item) }
        return result
    }
    if let array = value as? NSArray { return array.map { jsonValue($0) } }
    if let string = value as? String { return string }
    if let number = value as? NSNumber { return number }
    if value is NSNull { return NSNull() }
    return String(describing: value)
}

private func basicDescription(_ description: AudioStreamBasicDescription) -> [String: Any] {
    return ["formatID": fourCC(description.mFormatID),
            "formatIDHex": String(format: "0x%08X", description.mFormatID),
            "sampleRate": description.mSampleRate,
            "channelsPerFrame": description.mChannelsPerFrame,
            "formatFlags": description.mFormatFlags,
            "framesPerPacket": description.mFramesPerPacket]
}

private func listItem(_ item: AudioFormatListItem) -> [String: Any] {
    var result = basicDescription(item.mASBD)
    result["channelLayoutTag"] = item.mChannelLayoutTag
    result["channelLayoutTagHex"] = String(format: "0x%08X", item.mChannelLayoutTag)
    return result
}

private func formatDescription(_ format: CMFormatDescription, audio: Bool) -> [String: Any] {
    var result: [String: Any] = ["mediaSubType": fourCC(CMFormatDescriptionGetMediaSubType(format))]
    if let extensions = CMFormatDescriptionGetExtensions(format) {
        result["extensions"] = jsonValue(extensions)
    } else {
        result["extensions"] = [:] as [String: Any]
    }
    if audio {
        if let basic = CMAudioFormatDescriptionGetStreamBasicDescription(format) {
            result["basicDescription"] = basicDescription(basic.pointee)
        }
        var size = 0
        var declared: [[String: Any]] = []
        let formats = CMAudioFormatDescriptionGetFormatList(format, sizeOut: &size)
        let count = size / MemoryLayout<AudioFormatListItem>.size
        if let formats = formats {
            for index in 0..<count { declared.append(listItem(formats[index])) }
        }
        result["declaredFormatsRichestFirst"] = declared
        result["nativeDeclaresDolbyAtmosJOC"] = count > 0 && formats?.pointee.mASBD.mFormatID == ec3JOC
        if let richest = CMAudioFormatDescriptionGetRichestDecodableFormat(format) {
            result["richestDecodableFormat"] = listItem(richest.pointee)
            result["nativeCanDecodeDolbyAtmosJOC"] = richest.pointee.mASBD.mFormatID == ec3JOC
        } else {
            result["richestDecodableFormat"] = NSNull()
            result["nativeCanDecodeDolbyAtmosJOC"] = false
        }
        var layoutSize = 0
        if let layout = CMAudioFormatDescriptionGetChannelLayout(format, sizeOut: &layoutSize) {
            result["channelLayoutTag"] = layout.pointee.mChannelLayoutTag
            result["channelLayoutTagHex"] = String(format: "0x%08X", layout.pointee.mChannelLayoutTag)
        }
    } else {
        let dimensions = CMVideoFormatDescriptionGetDimensions(format)
        result["width"] = dimensions.width
        result["height"] = dimensions.height
        if let extensions = CMFormatDescriptionGetExtensions(format),
           let atoms = (extensions as NSDictionary)["SampleDescriptionExtensionAtoms"] as? NSDictionary {
            for atomName in ["dvvC", "dvcC"] {
                if let data = atoms[atomName] as? Data, data.count >= 5 {
                    let bytes = Array(data)
                    result["dolbyVisionConfiguration"] = [
                        "atom": atomName, "versionMajor": bytes[0], "versionMinor": bytes[1],
                        "profile": bytes[2] >> 1,
                        "level": ((bytes[2] & 1) << 5) | (bytes[3] >> 3),
                        "rpuPresent": (bytes[3] & 4) != 0,
                        "enhancementLayerPresent": (bytes[3] & 2) != 0,
                        "baseLayerPresent": (bytes[3] & 1) != 0,
                        "baseLayerCompatibilityID": bytes[4] >> 4
                    ] as [String: Any]
                    break
                }
            }
        }
    }
    return result
}

@main
private struct NativeMediaInspector {
    static func main() async {
        if Array(CommandLine.arguments.dropFirst()) == ["--help"] {
            print("Usage: apple_media_inspect FILE\nRead-only AVFoundation/CoreMedia inspection. JSON report; no playback or route changes.")
            return
        }
        do {
            guard CommandLine.arguments.count == 2 else {
                throw NSError(domain: "NativeMediaInspector", code: 64,
                              userInfo: [NSLocalizedDescriptionKey: "Usage: apple_media_inspect /path/to/movie.mp4"])
            }
            let url = URL(fileURLWithPath: CommandLine.arguments[1]).standardizedFileURL
            var isDirectory: ObjCBool = false
            guard FileManager.default.fileExists(atPath: url.path, isDirectory: &isDirectory),
                  !isDirectory.boolValue, FileManager.default.isReadableFile(atPath: url.path) else {
                throw NSError(domain: "NativeMediaInspector", code: 66,
                              userInfo: [NSLocalizedDescriptionKey: "Cannot read local video file: \(url.path)"])
            }
            let asset = AVURLAsset(url: url)
            let playable = try await asset.load(.isPlayable)
            let duration = try await asset.load(.duration)
            var tracks: [[String: Any]] = []
            var nativeAtmos = false
            var canDecodeAtmos = false
            var containsHDR = false
            var containsDolbyVision = false
            for mediaType in [AVMediaType.video, AVMediaType.audio] {
                let mediaTracks = try await asset.loadTracks(withMediaType: mediaType)
                for track in mediaTracks {
                    let descriptions = try await track.load(.formatDescriptions)
                    let characteristics = try await track.load(.mediaCharacteristics)
                    let formats = descriptions.map { formatDescription($0, audio: mediaType == .audio) }
                    let hdr = characteristics.contains(.containsHDRVideo)
                    containsHDR = containsHDR || hdr
                    nativeAtmos = nativeAtmos || formats.contains { $0["nativeDeclaresDolbyAtmosJOC"] as? Bool == true }
                    canDecodeAtmos = canDecodeAtmos || formats.contains { $0["nativeCanDecodeDolbyAtmosJOC"] as? Bool == true }
                    containsDolbyVision = containsDolbyVision || formats.contains { $0["dolbyVisionConfiguration"] != nil }
                    tracks.append(["trackID": track.trackID, "mediaType": mediaType.rawValue,
                                   "containsHDRVideo": hdr, "mediaCharacteristics": characteristics.map(\.rawValue),
                                   "formats": formats])
                }
            }
            let seconds = CMTimeGetSeconds(duration)
            let report: [String: Any] = ["schemaVersion": 1,
                                        "operation": "read_only_native_media_inspection",
                                        "path": url.path,
                                        "osVersion": ProcessInfo.processInfo.operatingSystemVersionString,
                                        "isPlayable": playable,
                                        "durationSeconds": seconds.isFinite ? seconds as Any : NSNull(),
                                        "nativeDeclaresDolbyAtmosJOC": nativeAtmos,
                                        "nativeCanDecodeDolbyAtmosJOC": canDecodeAtmos,
                                        "decoderProbeNote": "GetRichestDecodableFormat queries registered decoding formats. Verify the native player's Spatial Audio output separately.",
                                        "nativeContainsHDRVideo": containsHDR,
                                        "nativeContainsDolbyVisionConfiguration": containsDolbyVision,
                                        "avPlayerEligibleForHDRPlayback": AVPlayer.eligibleForHDRPlayback,
                                        "spatialAudioOutputVerified": false,
                                        "spatialAudioOutputVerification": "not_performed",
                                        "spatialAudioOutputNote": "Confirm Dolby Atmos and Fixed/Head Tracked in Control Center while using compatible headphones and a native player.",
                                        "tracks": tracks]
            let encoded = try JSONSerialization.data(withJSONObject: report, options: [.prettyPrinted, .sortedKeys, .withoutEscapingSlashes])
            FileHandle.standardOutput.write(encoded)
            FileHandle.standardOutput.write(Data([10]))
        } catch {
            let failure = ["error": error.localizedDescription]
            if let data = try? JSONSerialization.data(withJSONObject: failure, options: [.sortedKeys]) {
                FileHandle.standardOutput.write(data)
                FileHandle.standardOutput.write(Data([10]))
            }
            exit(1)
        }
    }
}
