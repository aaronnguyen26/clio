import AVFoundation
import AppKit
import Foundation
import Vision


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

var videoWidth: Double = 1440.0
var videoHeight: Double = 900.0

if let track = asset.tracks(withMediaType: .video).first {
    videoWidth = Double(track.naturalSize.width)
    videoHeight = Double(track.naturalSize.height)
    res["width"] = videoWidth
    res["height"] = videoHeight
    res["fps"] = track.nominalFrameRate
    res["has_video"] = true
} else {
    res["has_video"] = false
}

if let attributes = try? FileManager.default.attributesOfItem(atPath: path),
   let size = attributes[.size] as? Int64 {
    res["file_size_bytes"] = size
}

// Helper: recognize text on screen near target (x, y) using native Apple Vision framework
func recognizeTextNear(cgImg: CGImage, targetX: Double, targetY: Double, screenW: Double, screenH: Double) -> String? {
    let request = VNRecognizeTextRequest()
    request.recognitionLevel = .accurate
    request.usesLanguageCorrection = true

    let handler = VNImageRequestHandler(cgImage: cgImg, options: [:])
    try? handler.perform([request])
    guard let results = request.results, !results.isEmpty else { return nil }

    // Vision coordinates: origin (0, 0) at bottom-left, (1.0, 1.0) at top-right
    let normTargetX = max(0.0, min(1.0, targetX / max(1.0, screenW)))
    let normTargetY = max(0.0, min(1.0, 1.0 - (targetY / max(1.0, screenH))))
    let targetPt = CGPoint(x: normTargetX, y: normTargetY)

    var bestText: String? = nil
    var minDistance: Double = Double.infinity

    for obs in results {
        guard let candidate = obs.topCandidates(1).first else { continue }
        let text = candidate.string.trimmingCharacters(in: .whitespacesAndNewlines)
        if text.isEmpty || text.count > 60 { continue }

        let box = obs.boundingBox
        // Direct containment: point is inside or very close to bounding box
        let expanded = box.insetBy(dx: -0.025, dy: -0.025)
        if expanded.contains(targetPt) {
            return text
        }

        // Proximity match within ~100 normalized pixels
        let cx = box.midX
        let cy = box.midY
        let dist = hypot(cx - normTargetX, cy - normTargetY)
        if dist < 0.08 && dist < minDistance {
            minDistance = dist
            bestText = text
        }
    }

    return bestText
}

// Helper: compute pixel difference between pre-action and post-action frames around (x, y)
func computeVisualDelta(preImg: CGImage, postImg: CGImage, x: Double, y: Double, screenW: Double, screenH: Double) -> Double {
    let imgW = preImg.width
    let imgH = preImg.height
    guard imgW > 0 && imgH > 0 && postImg.width == imgW && postImg.height == imgH else { return 0.0 }

    let scaleX = Double(imgW) / max(1.0, screenW)
    let scaleY = Double(imgH) / max(1.0, screenH)
    let px = Int(x * scaleX)
    let py = Int(y * scaleY)

    let roiSize = 80
    let cropX = max(0, min(imgW - roiSize, px - roiSize / 2))
    let cropY = max(0, min(imgH - roiSize, py - roiSize / 2))
    let cropRect = CGRect(x: cropX, y: cropY, width: roiSize, height: roiSize)

    guard let preCrop = preImg.cropping(to: cropRect),
          let postCrop = postImg.cropping(to: cropRect) else { return 0.0 }

    let colorSpace = CGColorSpaceCreateDeviceRGB()
    var preBytes = [UInt8](repeating: 0, count: roiSize * roiSize * 4)
    var postBytes = [UInt8](repeating: 0, count: roiSize * roiSize * 4)

    let bitmapInfo = CGBitmapInfo(rawValue: CGImageAlphaInfo.premultipliedLast.rawValue)
    guard let ctxPre = CGContext(data: &preBytes, width: roiSize, height: roiSize, bitsPerComponent: 8, bytesPerRow: roiSize * 4, space: colorSpace, bitmapInfo: bitmapInfo.rawValue),
          let ctxPost = CGContext(data: &postBytes, width: roiSize, height: roiSize, bitsPerComponent: 8, bytesPerRow: roiSize * 4, space: colorSpace, bitmapInfo: bitmapInfo.rawValue) else { return 0.0 }

    ctxPre.draw(preCrop, in: CGRect(x: 0, y: 0, width: roiSize, height: roiSize))
    ctxPost.draw(postCrop, in: CGRect(x: 0, y: 0, width: roiSize, height: roiSize))

    var diffPixels = 0
    let totalPixels = roiSize * roiSize
    for i in 0..<totalPixels {
        let idx = i * 4
        let dr = abs(Int(preBytes[idx]) - Int(postBytes[idx]))
        let dg = abs(Int(preBytes[idx + 1]) - Int(postBytes[idx + 1]))
        let db = abs(Int(preBytes[idx + 2]) - Int(postBytes[idx + 2]))
        if (dr + dg + db) > 35 {
            diffPixels += 1
        }
    }

    return Double(diffPixels) / Double(totalPixels)
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

    // Check if actions.json path was provided as 3rd argument for deep video dissection
    var parsedActions: [[String: Any]] = []
    if CommandLine.arguments.count > 3 {
        let actionsPath = CommandLine.arguments[3]
        if let data = try? Data(contentsOf: URL(fileURLWithPath: actionsPath)),
           let json = try? JSONSerialization.jsonObject(with: data) {
            if let arr = json as? [[String: Any]] {
                parsedActions = arr
            } else if let dict = json as? [String: Any], let arr = dict["events"] as? [[String: Any]] {
                parsedActions = arr
            }
        }
    }

    var extractedFrames: [String] = []

    if !parsedActions.isEmpty {
        // Advanced Video Dissection Mode: Analyze video frames around each human interaction
        var actionsAnalysis: [[String: Any]] = []

        // Resolve recording start time offset if embedded in actions
        let firstActionT = (parsedActions.first?["timestamp"] as? Double) ?? 0.0
        let isAbsoluteTimestamp = (firstActionT > 1_000_000_000.0)

        for (idx, action) in parsedActions.enumerated() {
            let evType = (action["event_type"] as? String) ?? "click"
            let clickX = (action["x"] as? Double) ?? (action["screen_x"] as? Double) ?? 0.0
            let clickY = (action["y"] as? Double) ?? (action["screen_y"] as? Double) ?? 0.0
            let actionT = (action["timestamp"] as? Double) ?? 0.0

            let relTimeSec: Double
            if isAbsoluteTimestamp {
                relTimeSec = max(0.0, min(durationSec, actionT - firstActionT))
            } else {
                relTimeSec = max(0.0, min(durationSec, actionT))
            }

            let preT = max(0.0, relTimeSec - 0.15)
            let postT = min(durationSec, relTimeSec + 0.25)

            var preImg: CGImage? = nil
            var postImg: CGImage? = nil
            var preFramePath = ""
            var postFramePath = ""

            let preCmTime = CMTime(seconds: preT, preferredTimescale: 600)
            if let cg = try? generator.copyCGImage(at: preCmTime, actualTime: nil) {
                preImg = cg
                let rep = NSBitmapImageRep(cgImage: cg)
                if let jpeg = rep.representation(using: .jpeg, properties: [:]) {
                    let filename = String(format: "frame_%04d_pre.jpg", idx + 1)
                    let fileURL = framesDirURL.appendingPathComponent(filename)
                    try? jpeg.write(to: fileURL)
                    preFramePath = fileURL.path
                    extractedFrames.append(fileURL.path)
                }
            }

            let postCmTime = CMTime(seconds: postT, preferredTimescale: 600)
            if let cg = try? generator.copyCGImage(at: postCmTime, actualTime: nil) {
                postImg = cg
                let rep = NSBitmapImageRep(cgImage: cg)
                if let jpeg = rep.representation(using: .jpeg, properties: [:]) {
                    let filename = String(format: "frame_%04d_post.jpg", idx + 1)
                    let fileURL = framesDirURL.appendingPathComponent(filename)
                    try? jpeg.write(to: fileURL)
                    postFramePath = fileURL.path
                    extractedFrames.append(fileURL.path)
                }
            }

            // Run Apple Vision text recognition on the keyframe
            var recognizedText: String? = nil
            if let targetImg = postImg ?? preImg {
                recognizedText = recognizeTextNear(cgImg: targetImg, targetX: clickX, targetY: clickY, screenW: videoWidth, screenH: videoHeight)
            }

            // Compute visual delta
            var visualDelta = 0.0
            if let pre = preImg, let post = postImg {
                visualDelta = computeVisualDelta(preImg: pre, postImg: post, x: clickX, y: clickY, screenW: videoWidth, screenH: videoHeight)
            }

            var itemAnalysis: [String: Any] = [
                "order": idx + 1,
                "event_type": evType,
                "x": clickX,
                "y": clickY,
                "timestamp": actionT,
                "rel_time": round(relTimeSec * 100.0) / 100.0,
                "visual_delta": round(visualDelta * 1000.0) / 1000.0,
                "visual_change_confirmed": (visualDelta > 0.015),
            ]
            if let text = recognizedText, !text.isEmpty {
                itemAnalysis["recognized_text"] = text
            }
            if !preFramePath.isEmpty {
                itemAnalysis["pre_frame"] = preFramePath
            }
            if !postFramePath.isEmpty {
                itemAnalysis["post_frame"] = postFramePath
            }

            actionsAnalysis.append(itemAnalysis)
        }

        res["actions_analysis"] = actionsAnalysis
    } else {
        // Default milestone extraction: sample start, midpoint, and end
        var targetTimes: [Double] = []
        if durationSec > 0.5 {
            targetTimes = [0.0, durationSec * 0.5, max(0.1, durationSec - 0.1)]
        } else {
            targetTimes = [0.0]
        }

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
    }

    res["extracted_frames"] = extractedFrames
}

if let jsonData = try? JSONSerialization.data(withJSONObject: res, options: []),
   let jsonStr = String(data: jsonData, encoding: .utf8) {
    print(jsonStr)
}
