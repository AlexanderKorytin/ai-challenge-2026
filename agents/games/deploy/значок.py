"""Значок `.icns` «Игрового агента» из символа 🎮: AppKit рисует символ в нужных размерах,
`iconutil` собирает набор. Запуск: `python значок.py <путь.icns>`."""

import subprocess
import sys
import tempfile
from pathlib import Path

from AppKit import NSBitmapImageRep, NSFont, NSFontAttributeName, NSGraphicsContext, NSPNGFileType, NSString
from Foundation import NSMakePoint

РАЗМЕРЫ = (16, 32, 128, 256, 512)


def нарисовать(сторона: int, путь: Path) -> None:
    картинка = NSBitmapImageRep.alloc().initWithBitmapDataPlanes_pixelsWide_pixelsHigh_bitsPerSample_samplesPerPixel_hasAlpha_isPlanar_colorSpaceName_bytesPerRow_bitsPerPixel_(
        None, сторона, сторона, 8, 4, True, False, "NSCalibratedRGBColorSpace", 0, 0
    )
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.setCurrentContext_(NSGraphicsContext.graphicsContextWithBitmapImageRep_(картинка))
    шрифт = NSFont.systemFontOfSize_(сторона * 0.8)
    NSString.stringWithString_("🎮").drawAtPoint_withAttributes_(
        NSMakePoint(сторона * 0.05, сторона * 0.02), {NSFontAttributeName: шрифт}
    )
    NSGraphicsContext.restoreGraphicsState()
    путь.write_bytes(bytes(картинка.representationUsingType_properties_(NSPNGFileType, None)))


def main(цель: str) -> None:
    with tempfile.TemporaryDirectory() as каталог:
        набор = Path(каталог) / "значок.iconset"
        набор.mkdir()
        for р in РАЗМЕРЫ:
            нарисовать(р, набор / f"icon_{р}x{р}.png")
            нарисовать(р * 2, набор / f"icon_{р}x{р}@2x.png")
        subprocess.run(["iconutil", "-c", "icns", str(набор), "-o", цель], check=True)


if __name__ == "__main__":
    main(sys.argv[1])
