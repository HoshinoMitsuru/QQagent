# -*- coding: utf-8 -*-
"""
build_icon.py —— 生成 exe 与托盘用的 .ico

不引 Pillow：为了一个图标多一个几十 MB 的构建期依赖不划算，而 ICO 的格式本身
就是「文件头 + 若干张 BMP」——自己写反而更可控，也能顺手把抗锯齿做了
（在 4 倍尺寸上画再用面积平均降采样，效果比 Windows 自带的缩放好得多）。

产出：app/assets/qq-agent.ico
"""

from __future__ import annotations

import os
import struct

SIZES = (16, 24, 32, 48, 64, 128, 256)
SUPERSAMPLE = 4

BG = (59, 110, 245, 255)          # 品牌蓝
FG = (255, 255, 255, 255)         # 白色图形
TRANSPARENT = (0, 0, 0, 0)


def _in_rounded_rect(x: float, y: float, x0: float, y0: float,
                     x1: float, y1: float, r: float) -> bool:
    if x < x0 or x > x1 or y < y0 or y > y1:
        return False
    cx = min(max(x, x0 + r), x1 - r)
    cy = min(max(y, y0 + r), y1 - r)
    dx, dy = x - cx, y - cy
    return dx * dx + dy * dy <= r * r


def _in_circle(x: float, y: float, cx: float, cy: float, r: float) -> bool:
    dx, dy = x - cx, y - cy
    return dx * dx + dy * dy <= r * r


def _in_capsule(x: float, y: float, x0: float, y0: float, x1: float, y1: float,
                r: float) -> bool:
    """一条有粗细的线段（天线用）。"""
    vx, vy = x1 - x0, y1 - y0
    length2 = vx * vx + vy * vy
    if length2 == 0:
        return _in_circle(x, y, x0, y0, r)
    t = max(0.0, min(1.0, ((x - x0) * vx + (y - y0) * vy) / length2))
    return _in_circle(x, y, x0 + t * vx, y0 + t * vy, r)


def _sample(u: float, v: float) -> tuple:
    """归一化坐标 (0..1) 上的一个采样点，返回 RGBA。"""
    # 背景圆角方块
    if not _in_rounded_rect(u, v, 0.0, 0.0, 1.0, 1.0, 0.22):
        return TRANSPARENT
    # 天线
    if _in_capsule(u, v, 0.5, 0.32, 0.5, 0.40, 0.028) or _in_circle(u, v, 0.5, 0.275, 0.055):
        return FG
    # 头
    if _in_rounded_rect(u, v, 0.295, 0.415, 0.705, 0.775, 0.075):
        # 眼睛（挖成背景色）
        if _in_circle(u, v, 0.415, 0.565, 0.038) or _in_circle(u, v, 0.585, 0.565, 0.038):
            return BG
        # 嘴
        if _in_rounded_rect(u, v, 0.435, 0.665, 0.565, 0.705, 0.02):
            return BG
        return FG
    return BG


def render(size: int) -> bytes:
    """渲染一张 size×size 的 BGRA（自下而上）像素块。"""
    n = size * SUPERSAMPLE
    # 先在放大图上采样
    acc = [[[0, 0, 0, 0] for _ in range(size)] for _ in range(size)]
    for py in range(n):
        v = (py + 0.5) / n
        row = acc[py // SUPERSAMPLE]
        for px in range(n):
            u = (px + 0.5) / n
            r, g, b, a = _sample(u, v)
            cell = row[px // SUPERSAMPLE]
            cell[0] += r * a
            cell[1] += g * a
            cell[2] += b * a
            cell[3] += a
    k = SUPERSAMPLE * SUPERSAMPLE
    out = bytearray()
    for py in range(size - 1, -1, -1):            # BMP 行序自下而上
        for px in range(size):
            sr, sg, sb, sa = acc[py][px]
            if sa == 0:
                out += b"\x00\x00\x00\x00"
                continue
            # sr/sg/sb 里累加的是「颜色 × alpha」，所以颜色要除以 alpha 之和，
            # 不能除以采样数 —— 否则边缘一圈会算成接近纯黑的脏边。
            r = min(255, int(sr / sa + 0.5))
            g = min(255, int(sg / sa + 0.5))
            b = min(255, int(sb / sa + 0.5))
            a = min(255, int(sa / k + 0.5))
            out += bytes((b, g, r, a))
    return bytes(out)


def build_ico(path: str) -> str:
    images = []
    for s in SIZES:
        pixels = render(s)
        header = struct.pack("<IiiHHIIiiII", 40, s, s * 2, 1, 32, 0, 0, 0, 0, 0, 0)
        mask_row = ((s + 31) // 32) * 4
        mask = b"\x00" * (mask_row * s)
        images.append(header + pixels + mask)

    out = bytearray(struct.pack("<HHH", 0, 1, len(images)))
    offset = 6 + 16 * len(images)
    for s, data in zip(SIZES, images):
        out += struct.pack("<BBBBHHII",
                           0 if s >= 256 else s, 0 if s >= 256 else s,
                           0, 0, 1, 32, len(data), offset)
        offset += len(data)
    for data in images:
        out += data

    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(out)
    return path


def project_root() -> str:
    """
    找到「含有 app/ 的那一层」，也就是仓库根。

    ## 为什么要找，而不是直接拼 `__file__` 的上级

    这个脚本原先在仓库根，产出写成 `<自己所在目录>/app/assets/qq-agent.ico` 是对的。
    后来它被收进了 `archive/`（探针与构建辅助统一归档），那句拼法就变成了
    `<根>/archive/app/assets/…` —— 图标照样生成成功、退出码照样是 0，
    只是**生成到了一个没人看的地方**，而 build.bat 里 `if exist 图标` 那一句
    因为找不到文件会静默降级（不带图标继续构建）。这种「成功但没生效」最难查。

    所以改成往上找一层：谁含有 `app/`，谁就是根。这样脚本放在根或 archive/ 都对。
    """
    here = os.path.dirname(os.path.abspath(__file__))
    for cand in (here, os.path.dirname(here)):
        if os.path.isdir(os.path.join(cand, "app")):
            return cand
    return os.path.dirname(here)


if __name__ == "__main__":
    target = os.path.join(project_root(), "app", "assets", "qq-agent.ico")
    build_ico(target)
    print(f"已生成图标：{target}（{os.path.getsize(target)} 字节，"
          f"{len(SIZES)} 个尺寸：{', '.join(map(str, SIZES))}）")
