import AVFoundation
import Foundation

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

if let jsonData = try? JSONSerialization.data(withJSONObject: res, options: []),
   let jsonStr = String(data: jsonData, encoding: .utf8) {
    print(jsonStr)
}
