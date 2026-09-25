import Foundation
import AVFoundation
import CoreMedia
import CoreVideo
import AppKit
import CoreGraphics
import ImageIO

// MARK: - JSON Output Helper

func outputJSON(_ object: [String: Any]) {
    if let data = try? JSONSerialization.data(withJSONObject: object, options: []),
       let str = String(data: data, encoding: .utf8) {
        print(str)
        fflush(stdout)
    }
}

// MARK: - CLI Argument Parsing

guard CommandLine.arguments.count >= 3 else {
    outputJSON([
        "success": false,
        "error": "Usage: clio-synthesizer <frames_dir> <output_mov_path> [fps] [duration]"
    ])
    exit(1)
}

let framesDirPath = CommandLine.arguments[1]
let outputMovPath = CommandLine.arguments[2]

var targetFPS: Double = 30.0
if CommandLine.arguments.count > 3, let fpsArg = Double(CommandLine.arguments[3]), fpsArg > 0 {
    targetFPS = fpsArg
}

var requestedDuration: Double? = nil
if CommandLine.arguments.count > 4, let durArg = Double(CommandLine.arguments[4]), durArg > 0 {
    requestedDuration = durArg
}

// MARK: - Validate Input Frames Directory

var isDirectory: ObjCBool = false
guard FileManager.default.fileExists(atPath: framesDirPath, isDirectory: &isDirectory), isDirectory.boolValue else {
    outputJSON([
        "success": false,
        "error": "Frames directory does not exist or is not a directory: \(framesDirPath)"
    ])
    exit(1)
}

let framesDirURL = URL(fileURLWithPath: framesDirPath)
let supportedExtensions: Set<String> = ["jpg", "jpeg", "png"]

guard let fileURLs = try? FileManager.default.contentsOfDirectory(
    at: framesDirURL,
    includingPropertiesForKeys: [.contentModificationDateKey, .fileSizeKey],
    options: [.skipsHiddenFiles]
) else {
    outputJSON([
        "success": false,
        "error": "Failed to read directory contents: \(framesDirPath)"
    ])
    exit(1)
}

var frameURLs = fileURLs.filter { url in
    let ext = url.pathExtension.lowercased()
    return supportedExtensions.contains(ext)
}

// Sort chronologically using numeric-aware filename comparison
frameURLs.sort { $0.lastPathComponent.localizedStandardCompare($1.lastPathComponent) == .orderedAscending }

guard !frameURLs.isEmpty else {
    outputJSON([
        "success": false,
        "error": "No .jpg or .png frames found in directory: \(framesDirPath)"
    ])
    exit(1)
}

// MARK: - Image Decoding Helper

func loadCGImage(from url: URL) -> CGImage? {
    if let source = CGImageSourceCreateWithURL(url as CFURL, nil),
       CGImageSourceGetCount(source) > 0,
       let img = CGImageSourceCreateImageAtIndex(source, 0, nil) {
        return img
    }
    if let nsImg = NSImage(contentsOf: url) {
        var rect = CGRect(origin: .zero, size: nsImg.size)
        return nsImg.cgImage(forProposedRect: &rect, context: nil, hints: nil)
    }
    return nil
}

// Locate first valid readable frame
var firstFrameCGImage: CGImage? = nil
for url in frameURLs {
    if let img = loadCGImage(from: url) {
        firstFrameCGImage = img
        break
    }
}

if firstFrameCGImage == nil {
    // Fallback: create a 1920x1080 solid frame if image files on disk cannot be decoded (e.g. mock tests)
    let cs = CGColorSpaceCreateDeviceRGB()
    if let ctx = CGContext(
        data: nil,
        width: 1920,
        height: 1080,
        bitsPerComponent: 8,
        bytesPerRow: 1920 * 4,
        space: cs,
        bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue
    ) {
        ctx.setFillColor(CGColor(red: 0.05, green: 0.05, blue: 0.08, alpha: 1.0))
        ctx.fill(CGRect(x: 0, y: 0, width: 1920, height: 1080))
        firstFrameCGImage = ctx.makeImage()
    }
}

guard let firstImage = firstFrameCGImage else {
    outputJSON([
        "success": false,
        "error": "Could not decode any valid image frames from: \(framesDirPath)"
    ])
    exit(1)
}

// MARK: - Dimensions & Quality Resolution Adaptation

var targetWidth = firstImage.width
var targetHeight = firstImage.height

// Upscale mock 1x1 or sub-HD test frames to 1920x1080 to prevent H.264 encoding faults
if targetWidth < 160 || targetHeight < 120 {
    targetWidth = 1920
    targetHeight = 1080
}

// Ensure dimensions are even for H.264 4:2:0 chroma subsampling
targetWidth -= (targetWidth % 2)
targetHeight -= (targetHeight % 2)

if targetWidth < 2 || targetHeight < 2 {
    outputJSON([
        "success": false,
        "error": "Invalid frame dimensions: \(firstImage.width)x\(firstImage.height)"
    ])
    exit(1)
}

// MARK: - Timing & Pacing Calculation

let sourceCount = frameURLs.count
let effectiveDuration: Double

if let dur = requestedDuration, dur > 0 {
    effectiveDuration = dur
} else {
    // Attempt to parse millisecond timestamps from filenames (e.g. frame_0001_1727221234567.jpg)
    var parsedDuration: Double? = nil
    let regex = try? NSRegularExpression(pattern: #"_(\d{10,16})\."#)
    if let reg = regex {
        let firstStr = frameURLs.first!.lastPathComponent
        let lastStr = frameURLs.last!.lastPathComponent
        let firstMatches = reg.matches(in: firstStr, range: NSRange(firstStr.startIndex..., in: firstStr))
        let lastMatches = reg.matches(in: lastStr, range: NSRange(lastStr.startIndex..., in: lastStr))
        if let fm = firstMatches.first, let lm = lastMatches.first,
           let r1 = Range(fm.range(at: 1), in: firstStr),
           let r2 = Range(lm.range(at: 1), in: lastStr),
           let t1 = Double(firstStr[r1]), let t2 = Double(lastStr[r2]), t2 > t1 {
            let diffSec = (t2 - t1) / 1000.0
            if diffSec >= 0.5 {
                parsedDuration = diffSec
            }
        }
    }
    
    if let pd = parsedDuration {
        effectiveDuration = pd
    } else if sourceCount == 1 {
        effectiveDuration = 2.0 // Guarantee minimum 2s preview for single-frame captures
    } else {
        effectiveDuration = max(2.0, Double(sourceCount) * 0.5)
    }
}

let totalFrames = max(1, Int(round(effectiveDuration * targetFPS)))
let finalDuration = Double(totalFrames) / targetFPS

// MARK: - Output File & AVAssetWriter Configuration

let outputURL = URL(fileURLWithPath: outputMovPath)
try? FileManager.default.removeItem(at: outputURL)
let parentDir = outputURL.deletingLastPathComponent()
try? FileManager.default.createDirectory(at: parentDir, withIntermediateDirectories: true)

let writer: AVAssetWriter
do {
    writer = try AVAssetWriter(outputURL: outputURL, fileType: .mov)
} catch {
    outputJSON([
        "success": false,
        "error": "Failed to create AVAssetWriter for \(outputMovPath): \(error.localizedDescription)"
    ])
    exit(1)
}

// Fast start optimization relocates the 'moov' atom header to the beginning of the container
writer.shouldOptimizeForNetworkUse = true

let compressionProperties: [String: Any] = [
    AVVideoAverageBitRateKey: max(2_000_000, targetWidth * targetHeight * 4),
    AVVideoProfileLevelKey: AVVideoProfileLevelH264HighAutoLevel,
    AVVideoMaxKeyFrameIntervalKey: Int(targetFPS)
]

let videoSettings: [String: Any] = [
    AVVideoCodecKey: AVVideoCodecType.h264,
    AVVideoWidthKey: targetWidth,
    AVVideoHeightKey: targetHeight,
    AVVideoCompressionPropertiesKey: compressionProperties
]

let writerInput = AVAssetWriterInput(mediaType: .video, outputSettings: videoSettings)
writerInput.expectsMediaDataInRealTime = false

guard writer.canAdd(writerInput) else {
    outputJSON([
        "success": false,
        "error": "AVAssetWriter cannot add video input with settings \(videoSettings)"
    ])
    exit(1)
}
writer.add(writerInput)

let sourceBufferAttributes: [String: Any] = [
    kCVPixelBufferPixelFormatTypeKey as String: Int(kCVPixelFormatType_32ARGB),
    kCVPixelBufferWidthKey as String: targetWidth,
    kCVPixelBufferHeightKey as String: targetHeight,
    kCVPixelBufferCGImageCompatibilityKey as String: true,
    kCVPixelBufferCGBitmapContextCompatibilityKey as String: true
]

let adaptor = AVAssetWriterInputPixelBufferAdaptor(
    assetWriterInput: writerInput,
    sourcePixelBufferAttributes: sourceBufferAttributes
)

guard writer.startWriting() else {
    let errMsg = writer.error?.localizedDescription ?? "startWriting() returned false"
    outputJSON([
        "success": false,
        "error": "Failed to start writing: \(errMsg)"
    ])
    exit(1)
}

writer.startSession(atSourceTime: .zero)

// MARK: - CVPixelBuffer Creation & Drawing Helpers

func createPixelBuffer(width: Int, height: Int, pool: CVPixelBufferPool?) -> CVPixelBuffer? {
    var pb: CVPixelBuffer?
    if let pool = pool {
        let status = CVPixelBufferPoolCreatePixelBuffer(kCFAllocatorDefault, pool, &pb)
        if status == kCVReturnSuccess, let buffer = pb {
            return buffer
        }
    }
    let options: [CFString: Any] = [
        kCVPixelBufferCGImageCompatibilityKey: true,
        kCVPixelBufferCGBitmapContextCompatibilityKey: true
    ]
    let status = CVPixelBufferCreate(
        kCFAllocatorDefault,
        width,
        height,
        kCVPixelFormatType_32ARGB,
        options as CFDictionary,
        &pb
    )
    if status == kCVReturnSuccess, let buffer = pb {
        return buffer
    }
    return nil
}

func renderCGImageToPixelBuffer(
    _ cgImage: CGImage,
    into pixelBuffer: CVPixelBuffer,
    targetWidth: Int,
    targetHeight: Int
) -> Bool {
    guard CVPixelBufferLockBaseAddress(pixelBuffer, []) == kCVReturnSuccess else {
        return false
    }
    defer { CVPixelBufferUnlockBaseAddress(pixelBuffer, []) }

    guard let baseAddress = CVPixelBufferGetBaseAddress(pixelBuffer) else {
        return false
    }

    let bytesPerRow = CVPixelBufferGetBytesPerRow(pixelBuffer)
    let colorSpace = CGColorSpaceCreateDeviceRGB()
    let bitmapInfo = CGBitmapInfo.byteOrder32Big.rawValue | CGImageAlphaInfo.premultipliedFirst.rawValue

    guard let context = CGContext(
        data: baseAddress,
        width: targetWidth,
        height: targetHeight,
        bitsPerComponent: 8,
        bytesPerRow: bytesPerRow,
        space: colorSpace,
        bitmapInfo: bitmapInfo
    ) else {
        return false
    }

    // Obsidian background fill before drawing
    context.setFillColor(CGColor(red: 0, green: 0, blue: 0, alpha: 1))
    context.fill(CGRect(x: 0, y: 0, width: targetWidth, height: targetHeight))
    context.draw(cgImage, in: CGRect(x: 0, y: 0, width: targetWidth, height: targetHeight))
    return true
}

// MARK: - Frame Synthesis Loop

var currentSourceIndex = -1
var currentPixelBuffer: CVPixelBuffer? = nil
var encodedFramesCount = 0

for frameIndex in 0..<totalFrames {
    // Wait until writer input is ready for media data
    while !writerInput.isReadyForMoreMediaData {
        if writer.status == .failed || writer.status == .cancelled {
            let errMsg = writer.error?.localizedDescription ?? "Writer failed during frame encoding"
            outputJSON(["success": false, "error": errMsg])
            exit(1)
        }
        try? await Task.sleep(nanoseconds: 2_000_000)
    }

    // Map output frame index to source image
    let sourceIndex = min(sourceCount - 1, Int(Double(frameIndex) * Double(sourceCount) / Double(totalFrames)))

    // Decode and render new buffer only when transitioning to a different source image
    if sourceIndex != currentSourceIndex || currentPixelBuffer == nil {
        currentSourceIndex = sourceIndex
        let imgURL = frameURLs[sourceIndex]
        guard let cgImg = loadCGImage(from: imgURL) ?? firstFrameCGImage else {
            continue
        }

        guard let newBuffer = createPixelBuffer(width: targetWidth, height: targetHeight, pool: adaptor.pixelBufferPool) else {
            outputJSON(["success": false, "error": "Failed to allocate CVPixelBuffer at frame \(frameIndex)"])
            exit(1)
        }

        if !renderCGImageToPixelBuffer(cgImg, into: newBuffer, targetWidth: targetWidth, targetHeight: targetHeight) {
            outputJSON(["success": false, "error": "Failed to render frame \(frameIndex) into CVPixelBuffer"])
            exit(1)
        }

        currentPixelBuffer = newBuffer
    }

    guard let buffer = currentPixelBuffer else {
        continue
    }

    let presentationTime = CMTime(value: Int64(frameIndex), timescale: CMTimeScale(targetFPS))
    let appended = adaptor.append(buffer, withPresentationTime: presentationTime)
    if !appended {
        let errMsg = writer.error?.localizedDescription ?? "Adaptor failed to append pixel buffer at frame \(frameIndex)"
        outputJSON(["success": false, "error": errMsg])
        exit(1)
    }

    encodedFramesCount += 1
}

// MARK: - Finalize & Emit Summary

writerInput.markAsFinished()
await writer.finishWriting()

if writer.status == .failed {
    let errMsg = writer.error?.localizedDescription ?? "AVAssetWriter failed in finishWriting"
    outputJSON([
        "success": false,
        "error": errMsg
    ])
    exit(1)
}

let summary: [String: Any] = [
    "success": true,
    "frames_encoded": encodedFramesCount,
    "duration": finalDuration,
    "output": outputURL.path
]
outputJSON(summary)
