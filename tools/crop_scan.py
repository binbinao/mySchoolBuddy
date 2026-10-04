#!/usr/bin/env python3
"""crop_scan.py — 从试卷原卷扫描件里裁出指定区域并放大

**为什么不用 sips**：`sips --cropOffset y x -c 高 宽` 在本机 macOS 上
**参数顺序不生效**——写 `-c 640 1240 --cropOffset 620 0` 与
`--cropOffset 640 0 -c 640 1240` 裁出来的都是同一段，
我为此白读了两次同一页才发现。
`--cropOffset` 只对 JPG 生效，对 GIF 完全无效（人人文库那批是 GIF）。

**用法**（一次性工具，用完可删）

```bash
PY=/Users/jiduobin/.workbuddy/binaries/python/envs/default/bin/python
$PY tools/crop_scan.py <图片> <offsetY> <高> [宽] [输出]
```

例：读第 3 页下半部分
```bash
$PY tools/crop_scan.py RAW/试卷库/原卷扫描/sh-2022-zk-math-b-p03.gif 620 640 /tmp/a.png
```

**为什么自带图像解码**：隔离 venv 里没有 PIL，也没有 numpy。
标准库无法解 PNG/GIF，所以这里用 `sips` 只做「格式转换 GIF→PNG」，
再交给系统自带的 CoreGraphics（`osascript`/Quartz 不便调用）——
最终方案是**先用 sips 转 PNG，再用纯 Python 解 PNG**（zlib + 手工反滤波）。
这比装 Pillow 更可控。
"""

import struct
import subprocess
import sys
import tempfile
import zlib


def read_png_size(path):
    with open(path, "rb") as f:
        d = f.read(26)
    if d[:8] != b"\x89PNG\r\n\x1a\n":
        return (0, 0)
    return (int.from_bytes(d[16:20], "big"),
            int.from_bytes(d[20:24], "big"))


def to_png(src, workdir):
    """GIF/JPG → PNG。sips 只做格式转换，不做裁切（它的 offset 不可靠）。"""
    out = f"{workdir}/conv.png"
    subprocess.run(["sips", "-s", "format", "png", src, "--out", out],
                   capture_output=True, check=True)
    return out


def png_decode(path):
    """纯 Python 解 PNG（只支持 8bit RGB/RGBA/灰度，无隔行）→ (w,h,pixels list of rows)"""
    with open(path, "rb") as f:
        data = f.read()
    assert data[:8] == b"\x89PNG\r\n\x1a\n", "不是 PNG"

    pos = 8
    idat = b""
    w = h = depth = ctype = None
    while pos < len(data):
        ln = int.from_bytes(data[pos:pos + 4], "big")
        typ = data[pos + 4:pos + 8]
        chunk = data[pos + 8:pos + 8 + ln]
        if typ == b"IHDR":
            w = int.from_bytes(chunk[0:4], "big")
            h = int.from_bytes(chunk[4:8], "big")
            depth = chunk[8]
            ctype = chunk[9]
            interlace = chunk[12]
            assert depth == 8, f"只支持 8 位深度，实际 {depth}"
            assert interlace == 0, "不支持隔行 PNG"
        elif typ == b"IDAT":
            idat += chunk
        elif typ == b"IEND":
            break
        pos += 12 + ln

    nch = {0: 1, 2: 3, 4: 2, 6: 4}[ctype]
    raw = zlib.decompress(idat)
    stride = w * nch
    out = bytearray(w * h * nch)
    prev = bytearray(stride)
    p = 0
    for y in range(h):
        ft = raw[p]
        p += 1
        line = bytearray(raw[p:p + stride])
        p += stride
        if ft == 1:                      # Sub
            for i in range(nch, stride):
                line[i] = (line[i] + line[i - nch]) & 0xFF
        elif ft == 2:                    # Up
            for i in range(stride):
                line[i] = (line[i] + prev[i]) & 0xFF
        elif ft == 3:                    # Average
            for i in range(stride):
                left = line[i - nch] if i >= nch else 0
                line[i] = (line[i] + ((left + prev[i]) >> 1)) & 0xFF
        elif ft == 4:                    # Paeth
            for i in range(stride):
                a = line[i - nch] if i >= nch else 0
                b = prev[i]
                c = prev[i - nch] if i >= nch else 0
                pa, pb, pc = abs(b - c), abs(a - c), abs(a + b - 2 * c)
                pr = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                line[i] = (line[i] + pr) & 0xFF
        out[y * stride:(y + 1) * stride] = line
        prev = line
    return w, h, nch, out


def png_encode(path, w, h, nch, pix, scale=1):
    """把像素写成 PNG。scale>1 时用最近邻放大（放大题面看得清）。"""
    ow, oh = w * scale, h * scale
    ctype = {1: 0, 2: 4, 3: 2, 4: 6}[nch]
    rows = bytearray()
    for y in range(oh):
        rows.append(0)                      # filter None
        sy = y // scale
        base = sy * w * nch
        row = bytearray()
        for x in range(ow):
            sx = x // scale
            o = base + sx * nch
            row += pix[o:o + nch]
        rows += row

    def chunk(t, d):
        c = struct.pack(">I", len(d)) + t + d
        return c + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", ow, oh, 8, ctype, 0, 0, 0)
    with open(path, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n")
        f.write(chunk(b"IHDR", ihdr))
        f.write(chunk(b"IDAT", zlib.compress(bytes(rows), 6)))
        f.write(chunk(b"IEND", b""))


def main():
    if len(sys.argv) < 4:
        print(__doc__)
        return 1
    src = sys.argv[1]
    offy = int(sys.argv[2])
    ch = int(sys.argv[3])
    cw = int(sys.argv[4]) if len(sys.argv) > 4 else 0
    dst = sys.argv[5] if len(sys.argv) > 5 else "/tmp/crop.png"
    scale = int(sys.argv[6]) if len(sys.argv) > 6 else 1

    with tempfile.TemporaryDirectory() as wd:
        png = to_png(src, wd)
        w, h, nch, pix = png_decode(png)
        x0 = 0
        y0 = max(0, min(offy, h - 1))
        y1 = min(h, y0 + ch)
        cw = cw or w
        cw = min(cw, w - x0)

        # 裁剪（逐行整段拷贝，含 nch 每像素字节）
        stride = w * nch
        out_stride = cw * nch
        buf = bytearray(out_stride * (y1 - y0))
        for yy in range(y1 - y0):
            s = (y0 + yy) * stride + x0 * nch
            buf[yy * out_stride:(yy + 1) * out_stride] = pix[s:s + out_stride]
        png_encode(dst, cw, y1 - y0, nch, buf, scale=scale)

    print(f"原图 {w}×{h} → 裁 [{x0},{y0}] {cw}×{y1 - y0}"
          + (f" ×{scale}倍" if scale > 1 else "") + f" → {dst}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
