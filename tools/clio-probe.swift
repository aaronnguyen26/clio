import AVFoundation
import AppKit
import Foundation

if CommandLine.arguments.count > 1 && CommandLine.arguments[1] == "--check-perms" {
    let preflight = CGPreflightScreenCaptureAccess()
    print("CGPreflightScreenCaptureAccess: \(preflight)")
    exit(0)
}

guard CommandLine.arguments.count > 1 else {
    let err = ["error": "Missing video file path"]
    if let data = try? JSONSerialization.data(withJSONObject: err, options: []),
       let str = String(data: data, encoding: .utf8) {
        print(str)
    }
    exit(1)
}

let path = CommandLine.arguments[1]
let url = URL(fileURLWithPath: path)

guard FileManager.default.fileExists(atPath: path) else {
    let err = ["error": "File not found", "path": path]
    if let data = try? JSONSerialization.data(withJSONObject: err, options: []),
       let str = String(data: data, encoding: .utf8) {
        print(str)
    }
    exit(1)
}

let asset = AVURLAsset(url: url)
var res: [String: Any] = [:]
res["duration"] = CMTimeGetSeconds(asset.duration)

if let track = asset.tracks(withMediaType: .video).first {
    res["width"] = track.naturalSize.width
    res["height"] = track.naturalSize.height
    res["fps"] = track.nominalFrameRate
    res["has_video"] = true
} else {
    res["has_video"] = false
}

if let attributes = try? FileManager.default.attributesOfItem(atPath: path),
   let size = attributes[.size] as? Int64 {
    res["file_size_bytes"] = size
}

if CommandLine.arguments.count > 2 {
    let framesDirPath = CommandLine.arguments[2]
    let framesDirURL = URL(fileURLWithPath: framesDirPath)
    try? FileManager.default.createDirectory(at: framesDirURL, withIntermediateDirectories: true)

    let generator = AVAssetImageGenerator(asset: asset)
    generator.appliesPreferredTrackTransform = true
    generator.requestedTimeToleranceBefore = .zero
    generator.requestedTimeToleranceAfter = .zero

    let durationSec = CMTimeGetSeconds(asset.duration)
    var targetTimes: [Double] = []
    if durationSec > 0.5 {
        targetTimes = [0.0, durationSec * 0.5, max(0.1, durationSec - 0.1)]
    } else {
        targetTimes = [0.0]
    }

    var extractedFrames: [String] = []
    for (idx, sec) in targetTimes.enumerated() {
        let cmTime = CMTime(seconds: sec, preferredTimescale: 600)
        if let cgImg = try? generator.copyCGImage(at: cmTime, actualTime: nil) {
            let bitmapRep = NSBitmapImageRep(cgImage: cgImg)
            if let jpegData = bitmapRep.representation(using: .jpeg, properties: [:]) {
                let frameFilename = String(format: "frame_%04d_%lld.jpg", idx + 1, Int64(Date().timeIntervalSince1970 * 1000) + Int64(idx * 500))
                let frameURL = framesDirURL.appendingPathComponent(frameFilename)
                try? jpegData.write(to: frameURL)
                extractedFrames.append(frameURL.path)
            }
        }
    }
    res["extracted_frames"] = extractedFrames
}

if let jsonData = try? JSONSerialization.data(withJSONObject: res, options: []),
   let jsonStr = String(data: jsonData, encoding: .utf8) {
    print(jsonStr)
}

