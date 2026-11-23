// hapr_encoder.swift — HAP R (BC7/BPTC RGBA) encoder

import Foundation
import AVFoundation
import CoreVideo
import VideoToolbox
import Accelerate   // vImagePermuteChannels_ARGB8888 for vectorised BGRA→RGBA swap
import Darwin

let BUILD_TAG = "hapr_enc v25"

func P(_ pct: Double, _ msg: String) { print(String(format:"PROGRESS %.4f \(msg)",pct)); fflush(stdout) }
func E(_ msg: String) { fputs("ERROR \(msg)\n", stderr); fflush(stderr) }
func I(_ msg: String) { fputs("INFO  \(msg)\n", stderr); fflush(stderr) }
func pad4(_ n: Int) -> Int { ((n+3)/4)*4 }

// Catch crashes and log the signal before dying so the error dialog shows it
func installCrashHandler() {
    for sig in [SIGSEGV, SIGABRT, SIGBUS, SIGILL, SIGFPE] {
        signal(sig) { s in
            fputs("CRASH signal=\(s)\n", stderr); fflush(stderr)
            signal(s, SIG_DFL); raise(s)
        }
    }
}

// ── BC7 Mode 6 encoder ────────────────────────────────────────────────────────
// Writes 16 bytes directly into `out` — no heap allocation.
// @inline(__always) lets the optimiser fold this into the concurrent block loop.
@inline(__always)
func encodeBC7Block(_ px: UnsafePointer<UInt8>, into out: UnsafeMutablePointer<UInt8>) {
    var minR=UInt8(255),maxR=UInt8(0),minG=UInt8(255),maxG=UInt8(0)
    var minB=UInt8(255),maxB=UInt8(0),minA=UInt8(255),maxA=UInt8(0)
    for i in 0..<16 {
        let r=px[i*4], g=px[i*4+1], b=px[i*4+2], a=px[i*4+3]
        if r<minR{minR=r}; if r>maxR{maxR=r}
        if g<minG{minG=g}; if g>maxG{maxG=g}
        if b<minB{minB=b}; if b>maxB{maxB=b}
        if a<minA{minA=a}; if a>maxA{maxA=a}
    }
    var lo = UInt64(0x40) // Mode 6, bit 6 = 1
    // BC7 Mode 6 layout: endpoints begin at bit 7 (right after the mode bit at bit 6)
    // R0[6:0]=bits7-13, R1=14-20, G0=21-27, G1=28-34, B0=35-41, B1=42-48, A0=49-55, A1=56-62, P0=63
    lo |= UInt64(minR>>1)<<7;  lo |= UInt64(maxR>>1)<<14
    lo |= UInt64(minG>>1)<<21; lo |= UInt64(maxG>>1)<<28
    lo |= UInt64(minB>>1)<<35; lo |= UInt64(maxB>>1)<<42
    lo |= UInt64(minA>>1)<<49; lo |= UInt64(maxA>>1)<<56
    // Project each pixel onto the endpoint axis (R,G,B only — alpha is constant 0xFF).
    // This is the least-squares optimal index assignment for BC7 Mode 6.
    let dR = Int(maxR) - Int(minR)
    let dG = Int(maxG) - Int(minG)
    let dB = Int(maxB) - Int(minB)
    let lenSq = dR*dR + dG*dG + dB*dB
    var hi = UInt64(0)
    for i in 0..<16 {
        let idx: UInt64
        if lenSq == 0 {
            idx = 0
        } else {
            let pr = Int(px[i*4+0]) - Int(minR)
            let pg = Int(px[i*4+1]) - Int(minG)
            let pb = Int(px[i*4+2]) - Int(minB)
            let dot = pr*dR + pg*dG + pb*dB
            // Round to nearest, clamp to [0,15]
            let k = (dot * 15 + lenSq / 2) / lenSq
            idx = UInt64(k < 0 ? 0 : (k > 15 ? 15 : k))
        }
        if i == 0 { hi |= (idx & 0x7) << 1 }
        else       { hi |= idx << (i*4) }
    }
    // Write two little-endian UInt64s directly into the caller's buffer — no heap alloc.
    UnsafeMutableRawPointer(out).storeBytes(of: lo.littleEndian, as: UInt64.self)
    UnsafeMutableRawPointer(out + 8).storeBytes(of: hi.littleEndian, as: UInt64.self)
}

// Writes BC7 data directly into `dest` (caller pre-allocates bx*by*16 bytes).
// Uses withUnsafeTemporaryAllocation for the 64-byte per-block scratch buffer so
// each concurrent iteration stays on the stack — no heap pressure inside the loop.
func compressBC7(_ rgba: Data, w: Int, h: Int, into dest: UnsafeMutablePointer<UInt8>) {
    let bx=(w+3)/4, by=(h+3)/4
    rgba.withUnsafeBytes { ip in
        guard let s=ip.bindMemory(to:UInt8.self).baseAddress else { return }
        DispatchQueue.concurrentPerform(iterations: by) { y in
            // Stack-allocate the 64-byte 4×4 RGBA scratch block — avoids a heap
            // malloc/free per block (≈130 K allocs/frame at 1080p).
            withUnsafeTemporaryAllocation(byteCount: 64, alignment: 1) { blkBuf in
                let blk = blkBuf.bindMemory(to: UInt8.self).baseAddress!
                for x in 0..<bx {
                    for py in 0..<4 { for px in 0..<4 {
                        let sy=min(y*4+py,h-1), sx=min(x*4+px,w-1)
                        let si=(sy*w+sx)*4, di=(py*4+px)*4
                        blk[di]=s[si]; blk[di+1]=s[si+1]; blk[di+2]=s[si+2]; blk[di+3]=s[si+3]
                    }}
                    encodeBC7Block(blk, into: dest+(y*bx+x)*16)
                }
            }
        }
    }
}

// ── Snappy compression ────────────────────────────────────────────────────────
// Real Snappy (hash-table LZ77 matcher, same algorithm as the reference/Go
// implementation). BC7 of real content — black areas, flat colour, repeated
// texture — compresses 10–15× this way, which is what Jokyo/ffmpeg produce.
// (v1 emitted one big literal, i.e. no compression at all.)

@inline(__always) func sLd32(_ p: UnsafePointer<UInt8>) -> UInt32 {
    var v: UInt32 = 0; memcpy(&v, p, 4); return v
}
@inline(__always) func sLd64(_ p: UnsafePointer<UInt8>) -> UInt64 {
    var v: UInt64 = 0; memcpy(&v, p, 8); return v
}
@inline(__always) func snappyHash(_ u: UInt32) -> Int {
    return Int((u &* 0x1e35a7bd) >> 18)          // 14-bit table
}

@inline(__always)
func snappyEmitLiteral(_ o: UnsafeMutablePointer<UInt8>, _ opIn: Int,
                       _ lit: UnsafePointer<UInt8>, _ len: Int) -> Int {
    var op = opIn
    let n = len - 1
    if n < 60 {
        o[op] = UInt8(n << 2); op += 1
    } else if n < 256 {
        o[op] = 0xF0; o[op+1] = UInt8(n); op += 2
    } else {                                        // block ≤ 64 KB so n ≤ 65535
        o[op] = 0xF4; o[op+1] = UInt8(n & 0xFF); o[op+2] = UInt8(n >> 8); op += 3
    }
    memcpy(o + op, lit, len)
    return op + len
}

@inline(__always)
func snappyEmitCopy2(_ o: UnsafeMutablePointer<UInt8>, _ op: Int, _ off: Int, _ len: Int) -> Int {
    o[op]   = UInt8(((len - 1) << 2) | 2)
    o[op+1] = UInt8(off & 0xFF)
    o[op+2] = UInt8(off >> 8)
    return op + 3
}

@inline(__always)
func snappyEmitCopy(_ o: UnsafeMutablePointer<UInt8>, _ opIn: Int, _ off: Int, _ lenIn: Int) -> Int {
    var op = opIn, len = lenIn
    while len >= 68 { op = snappyEmitCopy2(o, op, off, 64); len -= 64 }
    if len > 64     { op = snappyEmitCopy2(o, op, off, 60); len -= 60 }
    if len >= 12 || off >= 2048 { return snappyEmitCopy2(o, op, off, len) }
    o[op]   = UInt8(((off >> 8) << 5) | ((len - 4) << 2) | 1)
    o[op+1] = UInt8(off & 0xFF)
    return op + 2
}

func snappyCompressBlock(_ src: UnsafePointer<UInt8>, _ blen: Int,
                         _ table: UnsafeMutablePointer<UInt16>,
                         _ o: UnsafeMutablePointer<UInt8>, _ opIn: Int) -> Int {
    var op = opIn
    if blen < 17 { return snappyEmitLiteral(o, op, src, blen) }
    memset(table, 0, MemoryLayout<UInt16>.stride * (1 << 14))
    let sLimit = blen - 15
    var nextEmit = 0
    var s = 1
    var nextHash = snappyHash(sLd32(src + s))
    outer: while true {
        var skip = 32, nextS = s, cand = 0
        repeat {
            s = nextS
            let step = skip >> 5
            nextS = s + step
            skip += step
            if nextS > sLimit { break outer }
            cand = Int(table[nextHash])
            table[nextHash] = UInt16(s)
            nextHash = snappyHash(sLd32(src + nextS))
        } while sLd32(src + s) != sLd32(src + cand)

        op = snappyEmitLiteral(o, op, src + nextEmit, s - nextEmit)

        var x: UInt32 = 0
        repeat {
            let base = s
            s += 4
            var i = cand + 4
            while s < blen && src[i] == src[s] { i += 1; s += 1 }
            op = snappyEmitCopy(o, op, base - cand, s - base)
            nextEmit = s
            if s >= sLimit { break outer }
            let x64 = sLd64(src + s - 1)
            table[snappyHash(UInt32(truncatingIfNeeded: x64))] = UInt16(s - 1)
            let ch = snappyHash(UInt32(truncatingIfNeeded: x64 >> 8))
            cand = Int(table[ch])
            table[ch] = UInt16(s)
            x = UInt32(truncatingIfNeeded: x64 >> 8)
        } while x == sLd32(src + cand)

        nextHash = snappyHash(UInt32(truncatingIfNeeded: sLd64(src + s - 1) >> 16))
        s += 1
    }
    if nextEmit < blen {
        op = snappyEmitLiteral(o, op, src + nextEmit, blen - nextEmit)
    }
    return op
}

func snappyCompress(_ src: UnsafePointer<UInt8>, _ n: Int) -> Data {
    let cap = 32 + n + n / 6
    let o = UnsafeMutablePointer<UInt8>.allocate(capacity: cap)
    defer { o.deallocate() }
    let table = UnsafeMutablePointer<UInt16>.allocate(capacity: 1 << 14)
    defer { table.deallocate() }
    var op = 0
    var v = UInt(n)                                    // varint: uncompressed length
    while v >= 0x80 { o[op] = UInt8(truncatingIfNeeded: v) | 0x80; op += 1; v >>= 7 }
    o[op] = UInt8(v); op += 1
    var b = 0
    while b < n {
        let e = min(b + 65536, n)
        op = snappyCompressBlock(src + b, e - b, table, o, op)
        b = e
    }
    return Data(bytes: o, count: op)
}

// ── HAP section builder ───────────────────────────────────────────────────────
// Standard 4-byte header when size < 16 MB; 8-byte extended header otherwise
// (or when `extended` is forced — the outer 0xCC frame always uses extended
// to match Smart Preso reference files).
func hapSection(_ type: UInt8, _ body: Data, extended: Bool = false) -> Data {
    var out = Data()
    let sz = body.count
    if extended || sz >= (1 << 24) {
        out.append(0); out.append(0); out.append(0); out.append(type)
        out.append(UInt8(sz & 0xFF)); out.append(UInt8((sz>>8) & 0xFF))
        out.append(UInt8((sz>>16) & 0xFF)); out.append(UInt8((sz>>24) & 0xFF))
    } else {
        out.append(UInt8(sz & 0xFF)); out.append(UInt8((sz>>8) & 0xFF))
        out.append(UInt8((sz>>16) & 0xFF)); out.append(type)
    }
    out.append(body)
    return out
}

// ── Multi-chunk Snappy HAP R frame ────────────────────────────────────────────
// Section type 0xCC = multi-chunk Snappy BC7, matching Smart Preso reference
// files (confirmed from ffmpeg Lavc59 output analysis).
// Structure: outer 0xCC (extended) → decode-instructions 0x01 → compressor
// table 0x02 (0x0B=Snappy per chunk) + size table 0x03 → chunk payloads.
func buildHapRFrame(_ bc7: Data, numChunks: Int = 8) -> Data {
    let total    = bc7.count
    let chunkSz  = (total + numChunks - 1) / numChunks
    var chunks   = [Data](repeating: Data(), count: numChunks)
    var comps    = [UInt8](repeating: 0x0B, count: numChunks)
    // Compress the chunks in parallel. Any chunk that doesn't shrink is stored
    // uncompressed (compressor 0x0A), as the Hap spec allows.
    bc7.withUnsafeBytes { rp in
        guard let base = rp.bindMemory(to: UInt8.self).baseAddress else { return }
        chunks.withUnsafeMutableBufferPointer { cbuf in
            comps.withUnsafeMutableBufferPointer { kbuf in
                DispatchQueue.concurrentPerform(iterations: numChunks) { k in
                    let start = min(k * chunkSz, total)
                    let end   = min(start + chunkSz, total)
                    let len   = end - start
                    let c = snappyCompress(base + start, len)
                    if c.count < len {
                        cbuf[k] = c;  kbuf[k] = 0x0B
                    } else {
                        cbuf[k] = Data(bytes: base + start, count: len);  kbuf[k] = 0x0A
                    }
                }
            }
        }
    }
    // 0x02: one compressor byte per chunk (0x0B = Snappy, 0x0A = none)
    let compTable = Data(comps)
    // 0x03: uint32 LE size of each compressed chunk
    var sizeTable = Data()
    for c in chunks {
        let s = UInt32(c.count)
        sizeTable.append(UInt8(s & 0xFF)); sizeTable.append(UInt8((s>>8) & 0xFF))
        sizeTable.append(UInt8((s>>16) & 0xFF)); sizeTable.append(UInt8((s>>24) & 0xFF))
    }
    // 0x01: decode instructions container
    var decInst = Data()
    decInst.append(contentsOf: hapSection(0x02, compTable))
    decInst.append(contentsOf: hapSection(0x03, sizeTable))
    // Outer body: decode instructions + raw chunk payloads
    var body = Data()
    body.append(contentsOf: hapSection(0x01, decInst))
    for c in chunks { body.append(c) }
    // Outer frame section — always extended header
    return hapSection(0xCC, body, extended: true)
}

// ── Write a file directly (bypass AVAssetWriter entirely) ─────────────────────
// AVAssetWriter refuses custom codec types. Write a minimal QuickTime .mov by hand.

func writeU32BE(_ v: UInt32, to data: inout Data) {
    data.append(UInt8((v>>24)&0xFF)); data.append(UInt8((v>>16)&0xFF))
    data.append(UInt8((v>>8)&0xFF));  data.append(UInt8(v&0xFF))
}
func writeU16BE(_ v: UInt16, to data: inout Data) {
    data.append(UInt8((v>>8)&0xFF)); data.append(UInt8(v&0xFF))
}
func fourCC(_ s: String) -> UInt32 {
    let b = Array(s.utf8); return UInt32(b[0])<<24|UInt32(b[1])<<16|UInt32(b[2])<<8|UInt32(b[3])
}

// UInt64 offsets — HAP R 1080p files easily exceed 4 GB (UInt32 overflows at frame ~2072)
struct FrameInfo { let offset: UInt64; let size: UInt32; let pts: CMTime; let dur: CMTime }
struct AudioInfo { let sampleRate: Int32; let channels: Int32; let totalSamples: Int64; let dataOffset: UInt64 }

func writeU64BE(_ v: UInt64, to data: inout Data) {
    data.append(UInt8((v>>56)&0xFF)); data.append(UInt8((v>>48)&0xFF))
    data.append(UInt8((v>>40)&0xFF)); data.append(UInt8((v>>32)&0xFF))
    data.append(UInt8((v>>24)&0xFF)); data.append(UInt8((v>>16)&0xFF))
    data.append(UInt8((v>>8)&0xFF));  data.append(UInt8(v&0xFF))
}

// Read the first audio track from asset as 16-bit LE PCM into a temp file.
func readAudioPCM(asset: AVAsset, to tmpURL: URL) -> AudioInfo? {
    guard let aTrack = asset.tracks(withMediaType: .audio).first else { return nil }
    var sampleRate: Int32 = 44100
    var channels: Int32 = 2
    if let desc = aTrack.formatDescriptions.first,
       let basic = CMAudioFormatDescriptionGetStreamBasicDescription(desc as! CMFormatDescription) {
        if basic.pointee.mSampleRate > 0  { sampleRate = Int32(basic.pointee.mSampleRate) }
        if basic.pointee.mChannelsPerFrame > 0 { channels = Int32(basic.pointee.mChannelsPerFrame) }
    }
    guard let reader = try? AVAssetReader(asset: asset) else { return nil }
    let aOut = AVAssetReaderTrackOutput(track: aTrack, outputSettings: [
        AVFormatIDKey: kAudioFormatLinearPCM,
        AVLinearPCMBitDepthKey: 16,
        AVLinearPCMIsFloatKey: false,
        AVLinearPCMIsBigEndianKey: false,
        AVLinearPCMIsNonInterleaved: false,
    ])
    guard reader.canAdd(aOut) else { return nil }
    reader.add(aOut)
    guard reader.startReading() else {
        E("Audio reader start failed: \(reader.error?.localizedDescription ?? "unknown")")
        return nil
    }
    Thread.sleep(forTimeInterval: 0.05)
    try? FileManager.default.removeItem(at: tmpURL)
    FileManager.default.createFile(atPath: tmpURL.path, contents: nil)
    guard let tmpFile = FileHandle(forWritingAtPath: tmpURL.path) else { return nil }
    var totalSamples: Int64 = 0
    while let sb = aOut.copyNextSampleBuffer() {
        totalSamples += Int64(CMSampleBufferGetNumSamples(sb))
        if let bb = CMSampleBufferGetDataBuffer(sb) {
            var ptr: UnsafeMutablePointer<CChar>? = nil
            var len = 0
            CMBlockBufferGetDataPointer(bb, atOffset: 0, lengthAtOffsetOut: nil, totalLengthOut: &len, dataPointerOut: &ptr)
            if let p = ptr {
                do { try tmpFile.write(contentsOf: Data(bytes: p, count: len)) }
                catch { E("Audio temp write: \(error)"); tmpFile.closeFile(); try? FileManager.default.removeItem(at: tmpURL); return nil }
            }
        }
    }
    tmpFile.closeFile()
    guard totalSamples > 0 else { try? FileManager.default.removeItem(at: tmpURL); return nil }
    I("Audio: \(totalSamples) samples @ \(sampleRate)Hz \(channels)ch")
    return AudioInfo(sampleRate: sampleRate, channels: channels, totalSamples: totalSamples, dataOffset: 0)
}

// offsetBias is added to each frame's stored offset when writing co64 entries.
// Pass 0 to get moov size; pass (moovSize + mdatHeaderSize) for the real file.
func buildMOV(frames: [FrameInfo], w: Int, h: Int,
              timeScale: Int32, totalDur: Int64,
              offsetBias: UInt64 = 0,
              audio: AudioInfo? = nil) -> Data {
    var moov = Data()

    let audMovDur = audio.map { Int64(Double($0.totalSamples) / Double($0.sampleRate) * Double(timeScale)) } ?? 0
    let movDur = max(totalDur, audMovDur)

    // mvhd
    var mvhd = Data()
    writeU32BE(0, to: &mvhd) // version+flags
    writeU32BE(0, to: &mvhd) // creation time
    writeU32BE(0, to: &mvhd) // modification time
    writeU32BE(UInt32(timeScale), to: &mvhd) // time scale
    writeU32BE(UInt32(movDur), to: &mvhd)    // duration = max(video, audio)
    writeU32BE(0x00010000, to: &mvhd) // rate 1.0
    writeU16BE(0x0100, to: &mvhd)     // volume
    mvhd.append(contentsOf: [UInt8](repeating:0, count:10)) // reserved
    // matrix identity
    let matrix: [UInt32] = [0x00010000,0,0, 0,0x00010000,0, 0,0,0x40000000]
    for v in matrix { writeU32BE(v, to: &mvhd) }
    mvhd.append(contentsOf: [UInt8](repeating:0, count:24)) // pre-defined
    writeU32BE(audio != nil ? 3 : 2, to: &mvhd) // next track ID

    var mvhdBox = Data()
    writeU32BE(UInt32(mvhd.count+8), to: &mvhdBox)
    mvhdBox.append(contentsOf: "mvhd".utf8)
    mvhdBox.append(mvhd)
    moov.append(mvhdBox)

    // trak
    var trak = Data()

    // tkhd
    var tkhd = Data()
    writeU32BE(0x0000000F, to: &tkhd) // version=0, flags=track enabled+in movie+in preview
    writeU32BE(0, to: &tkhd); writeU32BE(0, to: &tkhd) // creation, modification
    writeU32BE(1, to: &tkhd) // track ID
    writeU32BE(0, to: &tkhd) // reserved
    writeU32BE(UInt32(totalDur), to: &tkhd) // duration
    writeU32BE(0, to: &tkhd); writeU32BE(0, to: &tkhd) // reserved
    writeU16BE(0, to: &tkhd) // layer
    writeU16BE(0, to: &tkhd) // alternate group
    writeU16BE(0, to: &tkhd) // volume (video=0)
    writeU16BE(0, to: &tkhd) // reserved
    for v in matrix { writeU32BE(v, to: &tkhd) }
    writeU32BE(UInt32(w)<<16, to: &tkhd) // width (fixed point)
    writeU32BE(UInt32(h)<<16, to: &tkhd) // height (fixed point)

    var tkhdBox = Data()
    writeU32BE(UInt32(tkhd.count+8), to: &tkhdBox)
    tkhdBox.append(contentsOf: "tkhd".utf8)
    tkhdBox.append(tkhd)
    trak.append(tkhdBox)

    // mdia
    var mdia = Data()

    // mdhd
    var mdhd = Data()
    writeU32BE(0, to: &mdhd)
    writeU32BE(0, to: &mdhd); writeU32BE(0, to: &mdhd)
    writeU32BE(UInt32(timeScale), to: &mdhd)
    writeU32BE(UInt32(totalDur), to: &mdhd)
    writeU16BE(0x55C4, to: &mdhd) // language (und)
    writeU16BE(0, to: &mdhd)
    var mdhdBox = Data(); writeU32BE(UInt32(mdhd.count+8),to:&mdhdBox); mdhdBox.append(contentsOf: "mdhd".utf8); mdhdBox.append(mdhd)
    mdia.append(mdhdBox)

    // hdlr
    var hdlr = Data()
    writeU32BE(0, to: &hdlr)
    writeU32BE(0, to: &hdlr) // pre-defined
    writeU32BE(fourCC("vide"), to: &hdlr)
    writeU32BE(0, to: &hdlr); writeU32BE(0, to: &hdlr); writeU32BE(0, to: &hdlr)
    hdlr.append(0) // empty name
    var hdlrBox = Data(); writeU32BE(UInt32(hdlr.count+8),to:&hdlrBox); hdlrBox.append(contentsOf: "hdlr".utf8); hdlrBox.append(hdlr)
    mdia.append(hdlrBox)

    // minf > vmhd + dinf + stbl
    var minf = Data()
    // vmhd
    var vmhd = Data(); writeU32BE(0x00000001,to:&vmhd); writeU16BE(0,to:&vmhd); for _ in 0..<3{writeU16BE(0,to:&vmhd)}
    var vmhdBox = Data(); writeU32BE(UInt32(vmhd.count+8),to:&vmhdBox); vmhdBox.append(contentsOf: "vmhd".utf8); vmhdBox.append(vmhd)
    minf.append(vmhdBox)
    // dinf > dref
    var dref = Data(); writeU32BE(0,to:&dref); writeU32BE(1,to:&dref)
    var url = Data(); writeU32BE(0x00000001,to:&url) // self-contained
    var urlBox = Data(); writeU32BE(UInt32(url.count+8),to:&urlBox); urlBox.append(contentsOf: "url ".utf8); urlBox.append(url)
    dref.append(urlBox)
    var drefBox = Data(); writeU32BE(UInt32(dref.count+8),to:&drefBox); drefBox.append(contentsOf: "dref".utf8); drefBox.append(dref)
    var dinf = Data(); dinf.append(drefBox)
    var dinfBox = Data(); writeU32BE(UInt32(dinf.count+8),to:&dinfBox); dinfBox.append(contentsOf: "dinf".utf8); dinfBox.append(dinf)
    minf.append(dinfBox)

    // stbl
    var stbl = Data()

    // stsd — sample description
    var stsd = Data(); writeU32BE(0,to:&stsd); writeU32BE(1,to:&stsd)
    var sampleEntry = Data()
    sampleEntry.append(contentsOf:[UInt8](repeating:0,count:6)) // reserved
    writeU16BE(1, to: &sampleEntry) // data ref index
    // video sample entry fields
    writeU16BE(0,to:&sampleEntry); writeU16BE(0,to:&sampleEntry) // pre-defined, reserved
    for _ in 0..<3 { writeU32BE(0,to:&sampleEntry) } // pre-defined
    writeU16BE(UInt16(w),to:&sampleEntry); writeU16BE(UInt16(h),to:&sampleEntry)
    writeU32BE(0x00480000,to:&sampleEntry); writeU32BE(0x00480000,to:&sampleEntry) // 72dpi
    writeU32BE(0,to:&sampleEntry) // reserved
    writeU16BE(1,to:&sampleEntry) // frame count
    sampleEntry.append(contentsOf:[UInt8](repeating:0,count:32)) // compressor name
    writeU16BE(0x0018,to:&sampleEntry) // depth
    let i16: Int16 = -1; withUnsafeBytes(of:i16.bigEndian){sampleEntry.append(contentsOf:$0)}
    var seBox = Data(); writeU32BE(UInt32(sampleEntry.count+8),to:&seBox)
    seBox.append(contentsOf: "Hap7".utf8)
    seBox.append(sampleEntry)
    stsd.append(seBox)
    var stsdBox = Data(); writeU32BE(UInt32(stsd.count+8),to:&stsdBox); stsdBox.append(contentsOf: "stsd".utf8); stsdBox.append(stsd)
    stbl.append(stsdBox)

    // stts — time-to-sample (constant duration per frame)
    // Derive frame duration from total video duration / frame count to avoid
    // division-by-zero when CMSampleBufferGetDuration returns kCMTimeInvalid.
    let durTS: Int32 = frames.isEmpty ? Int32(timeScale / 25)
                     : Int32(clamping: totalDur / Int64(frames.count))
    var stts = Data(); writeU32BE(0,to:&stts); writeU32BE(1,to:&stts)
    writeU32BE(UInt32(frames.count),to:&stts)
    writeU32BE(UInt32(bitPattern:durTS),to:&stts)
    var sttsBox = Data(); writeU32BE(UInt32(stts.count+8),to:&sttsBox); sttsBox.append(contentsOf: "stts".utf8); sttsBox.append(stts)
    stbl.append(sttsBox)

    // stsz — sample sizes
    var stsz = Data(); writeU32BE(0,to:&stsz); writeU32BE(0,to:&stsz)
    writeU32BE(UInt32(frames.count),to:&stsz)
    for f in frames { writeU32BE(f.size,to:&stsz) }
    var stszBox = Data(); writeU32BE(UInt32(stsz.count+8),to:&stszBox); stszBox.append(contentsOf: "stsz".utf8); stszBox.append(stsz)
    stbl.append(stszBox)

    // co64 — 64-bit chunk offsets (HAP R 1080p files exceed 4 GB; stco would overflow)
    // Absolute file offset = frame.offset + offsetBias (moov size + mdat header size)
    var co64 = Data(); writeU32BE(0,to:&co64); writeU32BE(UInt32(frames.count),to:&co64)
    for f in frames { writeU64BE(f.offset + offsetBias, to:&co64) }
    var co64Box = Data(); writeU32BE(UInt32(co64.count+8),to:&co64Box); co64Box.append(contentsOf: "co64".utf8); co64Box.append(co64)
    stbl.append(co64Box)

    // stsc — sample-to-chunk (1 sample per chunk)
    var stsc = Data(); writeU32BE(0,to:&stsc); writeU32BE(1,to:&stsc)
    writeU32BE(1,to:&stsc); writeU32BE(1,to:&stsc); writeU32BE(1,to:&stsc)
    var stscBox = Data(); writeU32BE(UInt32(stsc.count+8),to:&stscBox); stscBox.append(contentsOf: "stsc".utf8); stscBox.append(stsc)
    stbl.append(stscBox)

    var stblBox = Data(); writeU32BE(UInt32(stbl.count+8),to:&stblBox); stblBox.append(contentsOf: "stbl".utf8); stblBox.append(stbl)
    minf.append(stblBox)

    var minfBox = Data(); writeU32BE(UInt32(minf.count+8),to:&minfBox); minfBox.append(contentsOf: "minf".utf8); minfBox.append(minf)
    mdia.append(minfBox)

    var mdiaBox = Data(); writeU32BE(UInt32(mdia.count+8),to:&mdiaBox); mdiaBox.append(contentsOf: "mdia".utf8); mdiaBox.append(mdia)
    trak.append(mdiaBox)

    var trakBox = Data(); writeU32BE(UInt32(trak.count+8),to:&trakBox); trakBox.append(contentsOf: "trak".utf8); trakBox.append(trak)
    moov.append(trakBox)

    // ── Audio trak ────────────────────────────────────────────────────────────
    if let aud = audio {
        let audMovDurU = UInt32(Int64(Double(aud.totalSamples) / Double(aud.sampleRate) * Double(timeScale)))
        var aTrak = Data()

        // tkhd
        var aTkhd = Data()
        writeU32BE(0x0000000F, to: &aTkhd)
        writeU32BE(0, to: &aTkhd); writeU32BE(0, to: &aTkhd)
        writeU32BE(2, to: &aTkhd) // track ID 2
        writeU32BE(0, to: &aTkhd)
        writeU32BE(audMovDurU, to: &aTkhd) // duration in movie time scale
        writeU32BE(0, to: &aTkhd); writeU32BE(0, to: &aTkhd)
        writeU16BE(0, to: &aTkhd); writeU16BE(0, to: &aTkhd)
        writeU16BE(0x0100, to: &aTkhd) // volume = 1.0
        writeU16BE(0, to: &aTkhd)
        for v in matrix { writeU32BE(v, to: &aTkhd) }
        writeU32BE(0, to: &aTkhd); writeU32BE(0, to: &aTkhd) // width/height = 0 for audio
        var aTkhdBox = Data(); writeU32BE(UInt32(aTkhd.count+8),to:&aTkhdBox); aTkhdBox.append(contentsOf: "tkhd".utf8); aTkhdBox.append(aTkhd)
        aTrak.append(aTkhdBox)

        // mdia
        var aMdia = Data()

        // mdhd (audio time scale = sample rate)
        var aMdhd = Data()
        writeU32BE(0, to: &aMdhd); writeU32BE(0, to: &aMdhd); writeU32BE(0, to: &aMdhd)
        writeU32BE(UInt32(aud.sampleRate), to: &aMdhd)
        writeU32BE(UInt32(aud.totalSamples), to: &aMdhd) // duration in samples
        writeU16BE(0x55C4, to: &aMdhd); writeU16BE(0, to: &aMdhd)
        var aMdhdBox = Data(); writeU32BE(UInt32(aMdhd.count+8),to:&aMdhdBox); aMdhdBox.append(contentsOf: "mdhd".utf8); aMdhdBox.append(aMdhd)
        aMdia.append(aMdhdBox)

        // hdlr soun
        var aHdlr = Data()
        writeU32BE(0, to: &aHdlr); writeU32BE(0, to: &aHdlr)
        writeU32BE(fourCC("soun"), to: &aHdlr)
        writeU32BE(0, to: &aHdlr); writeU32BE(0, to: &aHdlr); writeU32BE(0, to: &aHdlr)
        aHdlr.append(0)
        var aHdlrBox = Data(); writeU32BE(UInt32(aHdlr.count+8),to:&aHdlrBox); aHdlrBox.append(contentsOf: "hdlr".utf8); aHdlrBox.append(aHdlr)
        aMdia.append(aHdlrBox)

        // minf
        var aMinf = Data()

        // smhd
        var smhd = Data(); writeU32BE(0,to:&smhd); writeU16BE(0,to:&smhd); writeU16BE(0,to:&smhd)
        var smhdBox = Data(); writeU32BE(UInt32(smhd.count+8),to:&smhdBox); smhdBox.append(contentsOf: "smhd".utf8); smhdBox.append(smhd)
        aMinf.append(smhdBox)

        // dinf > dref
        var aDref = Data(); writeU32BE(0,to:&aDref); writeU32BE(1,to:&aDref)
        var aUrl = Data(); writeU32BE(0x00000001,to:&aUrl)
        var aUrlBox = Data(); writeU32BE(UInt32(aUrl.count+8),to:&aUrlBox); aUrlBox.append(contentsOf: "url ".utf8); aUrlBox.append(aUrl)
        aDref.append(aUrlBox)
        var aDrefBox = Data(); writeU32BE(UInt32(aDref.count+8),to:&aDrefBox); aDrefBox.append(contentsOf: "dref".utf8); aDrefBox.append(aDref)
        var aDinf = Data(); aDinf.append(aDrefBox)
        var aDinfBox = Data(); writeU32BE(UInt32(aDinf.count+8),to:&aDinfBox); aDinfBox.append(contentsOf: "dinf".utf8); aDinfBox.append(aDinf)
        aMinf.append(aDinfBox)

        // stbl
        var aStbl = Data()

        // stsd: sowt (16-bit LE PCM)
        var aStsd = Data(); writeU32BE(0,to:&aStsd); writeU32BE(1,to:&aStsd)
        var sowt = Data()
        sowt.append(contentsOf: [UInt8](repeating:0,count:6)) // reserved
        writeU16BE(1, to: &sowt)                              // data ref index
        writeU16BE(0, to: &sowt)                              // version
        writeU16BE(0, to: &sowt)                              // revision
        writeU32BE(0, to: &sowt)                              // vendor
        writeU16BE(UInt16(aud.channels), to: &sowt)           // channel count
        writeU16BE(16, to: &sowt)                             // sample size
        writeU16BE(0, to: &sowt)                              // compression ID
        writeU16BE(0, to: &sowt)                              // packet size
        writeU32BE(UInt32(aud.sampleRate) << 16, to: &sowt)  // sample rate (16.16 fixed)
        var sowtBox = Data(); writeU32BE(UInt32(sowt.count+8),to:&sowtBox); sowtBox.append(contentsOf: "sowt".utf8); sowtBox.append(sowt)
        aStsd.append(sowtBox)
        var aStsdBox = Data(); writeU32BE(UInt32(aStsd.count+8),to:&aStsdBox); aStsdBox.append(contentsOf: "stsd".utf8); aStsdBox.append(aStsd)
        aStbl.append(aStsdBox)

        // stts: every sample has duration 1 (at sampleRate time scale)
        var aStts = Data(); writeU32BE(0,to:&aStts); writeU32BE(1,to:&aStts)
        writeU32BE(UInt32(aud.totalSamples),to:&aStts); writeU32BE(1,to:&aStts)
        var aSttsBox = Data(); writeU32BE(UInt32(aStts.count+8),to:&aSttsBox); aSttsBox.append(contentsOf: "stts".utf8); aSttsBox.append(aStts)
        aStbl.append(aSttsBox)

        // stsc: all samples in one chunk
        var aStsc = Data(); writeU32BE(0,to:&aStsc); writeU32BE(1,to:&aStsc)
        writeU32BE(1,to:&aStsc)                              // first chunk
        writeU32BE(UInt32(aud.totalSamples),to:&aStsc)       // samples per chunk
        writeU32BE(1,to:&aStsc)                              // sample desc index
        var aStscBox = Data(); writeU32BE(UInt32(aStsc.count+8),to:&aStscBox); aStscBox.append(contentsOf: "stsc".utf8); aStscBox.append(aStsc)
        aStbl.append(aStscBox)

        // stsz: constant sample size (channels × 2 bytes)
        var aStsz = Data(); writeU32BE(0,to:&aStsz)
        writeU32BE(UInt32(aud.channels)*2, to: &aStsz)       // constant sample size
        writeU32BE(UInt32(aud.totalSamples), to: &aStsz)
        var aStszBox = Data(); writeU32BE(UInt32(aStsz.count+8),to:&aStszBox); aStszBox.append(contentsOf: "stsz".utf8); aStszBox.append(aStsz)
        aStbl.append(aStszBox)

        // co64: single chunk — audio follows all video data in mdat
        var aCo64 = Data(); writeU32BE(0,to:&aCo64); writeU32BE(1,to:&aCo64)
        writeU64BE(aud.dataOffset, to:&aCo64)
        var aCo64Box = Data(); writeU32BE(UInt32(aCo64.count+8),to:&aCo64Box); aCo64Box.append(contentsOf: "co64".utf8); aCo64Box.append(aCo64)
        aStbl.append(aCo64Box)

        var aStblBox = Data(); writeU32BE(UInt32(aStbl.count+8),to:&aStblBox); aStblBox.append(contentsOf: "stbl".utf8); aStblBox.append(aStbl)
        aMinf.append(aStblBox)

        var aMinfBox = Data(); writeU32BE(UInt32(aMinf.count+8),to:&aMinfBox); aMinfBox.append(contentsOf: "minf".utf8); aMinfBox.append(aMinf)
        aMdia.append(aMinfBox)

        var aMdiaBox = Data(); writeU32BE(UInt32(aMdia.count+8),to:&aMdiaBox); aMdiaBox.append(contentsOf: "mdia".utf8); aMdiaBox.append(aMdia)
        aTrak.append(aMdiaBox)

        var aTrakBox = Data(); writeU32BE(UInt32(aTrak.count+8),to:&aTrakBox); aTrakBox.append(contentsOf: "trak".utf8); aTrakBox.append(aTrak)
        moov.append(aTrakBox)
    }

    var moovBox = Data(); writeU32BE(UInt32(moov.count+8),to:&moovBox); moovBox.append(contentsOf: "moov".utf8); moovBox.append(moov)

    // Return just the moov box — mdat is already on disk
    return moovBox
}

// ── Overwrite confirmation ────────────────────────────────────────────────────
// Shows a native macOS dialog when the output file already exists.
// Returns true = proceed, false = user cancelled.
func confirmOverwrite(_ url: URL) -> Bool {
    // Replace any double-quotes in the name to keep the AppleScript string valid.
    let name = url.lastPathComponent.replacingOccurrences(of: "\"", with: "'")
    let proc = Process()
    proc.executableURL = URL(fileURLWithPath: "/usr/bin/osascript")
    proc.arguments = ["-e",
        "display dialog \"\(name) already exists. Replace it?\" " +
        "with title \"File Already Exists\" buttons {\"Cancel\", \"Replace\"} default button \"Replace\""
    ]
    do {
        try proc.run()
        proc.waitUntilExit()
        // osascript exits 1 when user clicks Cancel (AppleScript raises an error), 0 for Replace.
        return proc.terminationStatus == 0
    } catch {
        // If osascript cannot launch for any reason, proceed with encode.
        I("confirmOverwrite: osascript unavailable (\(error)), proceeding")
        return true
    }
}

// ── Main encode ───────────────────────────────────────────────────────────────
func encode(input: URL, output: URL) -> Bool {
    if FileManager.default.fileExists(atPath: output.path) {
        if !confirmOverwrite(output) {
            E("Cancelled — output file not replaced")
            return false
        }
    }

    let asset = AVAsset(url: input)
    let g = DispatchGroup(); g.enter()
    asset.loadValuesAsynchronously(forKeys:["tracks","duration"]) { g.leave() }
    g.wait()

    guard let vTrack = asset.tracks(withMediaType:.video).first else {
        E("No video track"); return false
    }
    let totalSecs = CMTimeGetSeconds(asset.duration)
    let pw = pad4(Int(vTrack.naturalSize.width))
    let ph = pad4(Int(vTrack.naturalSize.height))
    let timeScale = Int32(vTrack.naturalTimeScale > 0 ? vTrack.naturalTimeScale : 600)
    I("\(pw)x\(ph) @ \(vTrack.nominalFrameRate)fps, \(String(format:"%.2f",totalSecs))s, timeScale=\(timeScale)")

    guard let reader = try? AVAssetReader(asset: asset) else { E("Cannot create reader"); return false }
    // AVAssetReader does not scale — omit width/height keys which cause "Cannot Decode"
    let pixelFormats: [OSType] = [
        kCVPixelFormatType_32BGRA,
        kCVPixelFormatType_32RGBA,
        kCVPixelFormatType_422YpCbCr8,
    ]
    var vOut: AVAssetReaderTrackOutput? = nil
    var chosenFmt: OSType = kCVPixelFormatType_32BGRA
    for fmt in pixelFormats {
        // Force software decoding — hardware (Metal/AGX) decoder crashes when
        // the process is a subprocess of a GUI app (no valid Metal display context).
        let candidate = AVAssetReaderTrackOutput(track: vTrack, outputSettings: [
            kCVPixelBufferPixelFormatTypeKey as String: fmt,
            AVVideoDecompressionPropertiesKey: [
                kVTVideoDecoderSpecification_EnableHardwareAcceleratedVideoDecoder as String: false
            ] as [String: Any],
        ])
        candidate.alwaysCopiesSampleData = false
        if reader.canAdd(candidate) { vOut = candidate; chosenFmt = fmt; break }
    }
    guard let vOut = vOut else { E("Reader cannot add any video output format"); return false }
    I("Using pixel format: \(chosenFmt)")
    reader.add(vOut)
    I("Starting reader...")
    guard reader.startReading() else {
        E("Reader startReading failed: status=\(reader.status.rawValue) error=\(reader.error?.localizedDescription ?? "none")")
        return false
    }
    I("Reader started successfully, status=\(reader.status.rawValue)")
    // Brief yield so the main run loop can finish any pending AVFoundation
    // setup dispatched internally by startReading() — without this, the first
    // copyNextSampleBuffer() call races against that initialization under -O.
    Thread.sleep(forTimeInterval: 0.05)

    // Moov-first (faststart) layout: [moov][mdat size=0][frame data]
    // Frame data written to a temp file first so we know all frame sizes
    // before computing co64 offsets and building moov.
    let tmpURL = URL(fileURLWithPath: output.path + ".hapr_tmp")
    try? FileManager.default.removeItem(at: tmpURL)
    FileManager.default.createFile(atPath: tmpURL.path, contents: nil)
    guard let tmpFile = FileHandle(forWritingAtPath: tmpURL.path) else {
        E("Cannot create temp file at \(tmpURL.path)"); return false
    }
    var frameByteOffset: UInt64 = 0

    // Pre-allocate per-frame RGBA and BC7 buffers once — avoids repeated large mallocs.
    // The HAP frame (Snappy multi-chunk) is built per-frame from the BC7 buffer.
    let bx = (pw+3)/4, by = (ph+3)/4
    let bc7Size = bx * by * 16
    var rgba   = Data(count: pw * ph * 4)
    var bc7Buf = Data(count: bc7Size)

    P(0, "Starting BC7 encode…")
    let t0 = Date()
    var frames = [FrameInfo]()
    var frameCount = 0

    while true {
        guard let sb = vOut.copyNextSampleBuffer() else {
            I("Reader finished after \(frameCount) frames. Reader status: \(reader.status.rawValue)")
            break
        }
        guard let pxb = CMSampleBufferGetImageBuffer(sb) else { continue }
        let pts = CMSampleBufferGetPresentationTimeStamp(sb)
        let dur = CMSampleBufferGetDuration(sb)

        CVPixelBufferLockBaseAddress(pxb, .readOnly)
        let bpr = CVPixelBufferGetBytesPerRow(pxb)
        if let base = CVPixelBufferGetBaseAddress(pxb) {
            let actualFmt = CVPixelBufferGetPixelFormatType(pxb)
            rgba.withUnsafeMutableBytes { dst in
                guard let d = dst.bindMemory(to:UInt8.self).baseAddress else { return }
                if actualFmt == kCVPixelFormatType_32BGRA {
                    // Vectorised BGRA→RGBA channel swap via vImage.
                    // permuteMap[i] = source channel index for destination channel i.
                    // BGRA source: B=0,G=1,R=2,A=3 → RGBA dest: R=src[2],G=src[1],B=src[0],A=src[3]
                    var srcBuf = vImage_Buffer(data: base,
                                               height: vImagePixelCount(ph),
                                               width:  vImagePixelCount(pw),
                                               rowBytes: bpr)
                    var dstBuf = vImage_Buffer(data: d,
                                               height: vImagePixelCount(ph),
                                               width:  vImagePixelCount(pw),
                                               rowBytes: pw*4)
                    let perm: [UInt8] = [2, 1, 0, 3]
                    vImagePermuteChannels_ARGB8888(&srcBuf, &dstBuf, perm, 0)
                    // Force alpha=0xFF — software decoders often emit A=0 for
                    // non-alpha content, which makes HAP R playback transparent.
                    for i in 0..<(pw*ph) { d[i*4+3] = 0xFF }
                } else {
                    let s = base.bindMemory(to:UInt8.self, capacity: ph*bpr)
                    for row in 0..<ph { memcpy(d + row*pw*4, s + row*bpr, pw*4) }
                    for i in 0..<(pw*ph) { d[i*4+3] = 0xFF }
                }
            }
        }
        CVPixelBufferUnlockBaseAddress(pxb, .readOnly)

        // Encode BC7 into the pre-allocated buffer, then wrap in a multi-chunk
        // Snappy HAP R frame (0xCC) matching Smart Preso's reference file format.
        bc7Buf.withUnsafeMutableBytes { bp in
            guard let p = bp.bindMemory(to:UInt8.self).baseAddress else { return }
            compressBC7(rgba, w: pw, h: ph, into: p)
        }
        let hapFrame = buildHapRFrame(bc7Buf)

        frames.append(FrameInfo(offset: frameByteOffset, size: UInt32(hapFrame.count), pts: pts, dur: dur))
        do { try tmpFile.write(contentsOf: hapFrame) }
        catch {
            E("Temp write failed at frame \(frameCount): \(error)")
            tmpFile.closeFile(); try? FileManager.default.removeItem(at: tmpURL); return false
        }
        frameByteOffset += UInt64(hapFrame.count)
        frameCount += 1

        if frameCount % 30 == 0 {
            let secs = CMTimeGetSeconds(pts)
            let pct  = totalSecs > 0 ? min(secs/totalSecs, 0.99) : 0
            let el   = Date().timeIntervalSince(t0)
            let hh=Int(secs/3600); let mm=Int(secs.truncatingRemainder(dividingBy:3600)/60)
            let ss=Int(secs.truncatingRemainder(dividingBy:60))
            let em=Int(el/60); let es=Int(el.truncatingRemainder(dividingBy:60))
            P(pct, String(format:"Encoding… %02d:%02d:%02d (%d%%) [%dm %02ds] frames=%d",
                           hh,mm,ss,Int(pct*100),em,es,frameCount))
        }
    }

    tmpFile.closeFile()
    if reader.status == .failed {
        E("Reader failed during decode: \(reader.error?.localizedDescription ?? "unknown")")
    }
    I("Loop ended: frameCount=\(frameCount) readerStatus=\(reader.status.rawValue)")
    guard frameCount > 0 else {
        E("No frames encoded"); try? FileManager.default.removeItem(at: tmpURL); return false
    }
    I("Encoded \(frameCount) frames, building MOV structure…")

    let totalDur = Int64(totalSecs * Double(timeScale))

    // Read audio into a temp file (separate AVAssetReader — video reader is done).
    let tmpAudioURL = URL(fileURLWithPath: output.path + ".hapr_audio_tmp")
    let rawAudio = readAudioPCM(asset: asset, to: tmpAudioURL)

    // Two-pass moov build: pass 1 gives exact size (audio dataOffset=0 placeholder),
    // pass 2 uses real offsets. Changing dataOffset value doesn't change moov size.
    let dummyMoov = buildMOV(frames: frames, w: pw, h: ph,
                              timeScale: timeScale, totalDur: totalDur,
                              offsetBias: 0, audio: rawAudio)
    let moovSize = UInt64(dummyMoov.count)
    let mdatHdrSize: UInt64 = 8   // 4-byte size=0 + 4-byte "mdat"

    // Audio chunk sits immediately after all video frame data.
    let audioInfo: AudioInfo? = rawAudio.map {
        AudioInfo(sampleRate: $0.sampleRate, channels: $0.channels,
                  totalSamples: $0.totalSamples,
                  dataOffset: moovSize + mdatHdrSize + frameByteOffset)
    }
    let moov = buildMOV(frames: frames, w: pw, h: ph,
                         timeScale: timeScale, totalDur: totalDur,
                         offsetBias: moovSize + mdatHdrSize,
                         audio: audioInfo)

    // Write final output: [moov][mdat size=0 extends-to-EOF][frame data from temp]
    try? FileManager.default.removeItem(at: output)
    FileManager.default.createFile(atPath: output.path, contents: nil)
    guard let outFile = FileHandle(forWritingAtPath: output.path) else {
        E("Cannot open output file"); try? FileManager.default.removeItem(at: tmpURL); return false
    }
    do { try outFile.write(contentsOf: moov) } catch {
        E("Output write failed (moov): \(error)")
        outFile.closeFile(); try? FileManager.default.removeItem(at: output); try? FileManager.default.removeItem(at: tmpURL); return false
    }
    var mdatHdr = Data()
    writeU32BE(0, to: &mdatHdr)           // size=0 → extends to EOF
    mdatHdr.append(contentsOf: "mdat".utf8)
    do { try outFile.write(contentsOf: mdatHdr) } catch {
        E("Output write failed (mdat): \(error)")
        outFile.closeFile(); try? FileManager.default.removeItem(at: output); try? FileManager.default.removeItem(at: tmpURL); return false
    }

    // Copy frame data from temp file using the throwing FileHandle constructor
    // so any open failure produces a real error message instead of a silent nil.
    I("Copying frame data from \(tmpURL.path)…")
    let tmpRead: FileHandle
    do { tmpRead = try FileHandle(forReadingFrom: tmpURL) } catch {
        E("Cannot open temp file for reading: \(error)")
        outFile.closeFile(); try? FileManager.default.removeItem(at: output); try? FileManager.default.removeItem(at: tmpURL); return false
    }
    let chunkSize = 8 * 1024 * 1024
    var copyOk = true
    copyLoop: while true {
        let chunk: Data?
        do { chunk = try tmpRead.read(upToCount: chunkSize) } catch {
            E("Temp read error: \(error)"); copyOk = false; break copyLoop
        }
        guard let chunk = chunk, !chunk.isEmpty else { break }
        do { try outFile.write(contentsOf: chunk) } catch {
            E("Output write error during copy: \(error)"); copyOk = false; break copyLoop
        }
    }
    tmpRead.closeFile()
    try? FileManager.default.removeItem(at: tmpURL)
    guard copyOk else {
        outFile.closeFile(); try? FileManager.default.removeItem(at: output)
        try? FileManager.default.removeItem(at: tmpAudioURL); return false
    }

    // Append audio PCM data after all video frames (if present).
    if audioInfo != nil {
        I("Appending audio…")
        if let tmpAudioRead = try? FileHandle(forReadingFrom: tmpAudioURL) {
            audioLoop: while true {
                let chunk: Data?
                do { chunk = try tmpAudioRead.read(upToCount: chunkSize) } catch {
                    E("Audio read error: \(error)"); break audioLoop
                }
                guard let chunk = chunk, !chunk.isEmpty else { break }
                do { try outFile.write(contentsOf: chunk) } catch {
                    E("Audio write error: \(error)"); break audioLoop
                }
            }
            tmpAudioRead.closeFile()
        } else {
            E("Cannot open audio temp for reading — audio omitted")
        }
        try? FileManager.default.removeItem(at: tmpAudioURL)
    }
    outFile.closeFile()

    let fileSize = (try? FileManager.default.attributesOfItem(atPath: output.path))?[.size] as? Int64 ?? 0
    guard fileSize > 0 else { E("Output file is empty after write!"); return false }

    let el = Date().timeIntervalSince(t0)
    P(1.0, String(format:"Done — %d frames, %.1f MB [%dm %02ds]",
                   frameCount, Double(fileSize)/1048576, Int(el/60), Int(el.truncatingRemainder(dividingBy:60))))
    return true
}

let args = CommandLine.arguments
installCrashHandler()
I(BUILD_TAG)
guard args.count == 3 else { fputs("Usage: hapr_encoder <input> <output>\n", stderr); exit(1) }
let inp = URL(fileURLWithPath: args[1])
let out = URL(fileURLWithPath: args[2])
guard FileManager.default.fileExists(atPath: inp.path) else { E("Not found: \(inp.path)"); exit(2) }

// AVAssetReader dispatches internal work back to the main run loop.
// Run encoding on a background thread and keep CFRunLoopRun() alive on main
// so those callbacks are serviced — required when launched as a subprocess
// from a GUI app (no implicit run loop from the shell session).
var exitCode: Int32 = 2
DispatchQueue.global(qos: .userInitiated).async {
    exitCode = encode(input: inp, output: out) ? 0 : 2
    CFRunLoopStop(CFRunLoopGetMain())
}
CFRunLoopRun()
exit(exitCode)
