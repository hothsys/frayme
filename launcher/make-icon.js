// Render logo.svg as a 1024x1024 icon PNG (with or without a rounded-square backdrop).
//
//   osascript -l JavaScript make-icon.js <logo.svg> <out.png> <mac|web|dialog>
//
// "mac" follows the macOS icon grid (824px square centered with margin for the
// system shadow); without it, macOS 26 wraps the icon in a gray frame.
// "web" fills the canvas so the logo stays legible at favicon sizes.
// "dialog" is the bare logo on a transparent background, for the launcher's
// "already running" dialog, where the rounded-square backdrop shows as a white box.
// Uses only AppKit, so any SVG/PNG that macOS can open works, including
// logos with transparency.

ObjC.import('AppKit');

function run(argv) {
  const [logoPath, outPath, mode] = argv;
  const logo = $.NSImage.alloc.initWithContentsOfFile(logoPath);
  if (logo.isNil()) throw new Error('Could not load ' + logoPath);

  const S = 1024;
  const layout = {
    mac:    { inset: 100, radius: 185, logoFrac: 0.78, background: true },
    web:    { inset: 0,   radius: 225, logoFrac: 0.84, background: true },
    dialog: { inset: 0,   radius: 0,   logoFrac: 0.99, background: false },
  }[mode];
  if (!layout) throw new Error('Unknown mode: ' + mode);

  const rep = $.NSBitmapImageRep.alloc
    .initWithBitmapDataPlanesPixelsWidePixelsHighBitsPerSampleSamplesPerPixelHasAlphaIsPlanarColorSpaceNameBytesPerRowBitsPerPixel(
      null, S, S, 8, 4, true, false, $.NSDeviceRGBColorSpace, 0, 0);
  const ctx = $.NSGraphicsContext.graphicsContextWithBitmapImageRep(rep);
  // Zero-argument ObjC methods are invoked by property access in JXA
  $.NSGraphicsContext.saveGraphicsState;
  $.NSGraphicsContext.setCurrentContext(ctx);
  ctx.imageInterpolation = $.NSImageInterpolationHigh;

  // Background: white fading to a light blue-gray
  const side = S - 2 * layout.inset;
  if (layout.background) {
    const square = $.NSBezierPath.bezierPathWithRoundedRectXRadiusYRadius(
      $.NSMakeRect(layout.inset, layout.inset, side, side), layout.radius, layout.radius);
    const gradient = $.NSGradient.alloc.initWithStartingColorEndingColor(
      $.NSColor.colorWithSRGBRedGreenBlueAlpha(0xe4 / 255, 0xe9 / 255, 0xf2 / 255, 1),
      $.NSColor.colorWithSRGBRedGreenBlueAlpha(1, 1, 1, 1));
    gradient.drawInBezierPathAngle(square, 90);
  }

  // Logo: fit inside a centered box, preserving aspect ratio
  const box = side * layout.logoFrac;
  const ls = logo.size;
  const scale = Math.min(box / ls.width, box / ls.height);
  const w = ls.width * scale, h = ls.height * scale;
  logo.drawInRectFromRectOperationFraction(
    $.NSMakeRect((S - w) / 2, (S - h) / 2, w, h),
    $.NSZeroRect, $.NSCompositingOperationSourceOver, 1.0);

  ctx.flushGraphics;
  $.NSGraphicsContext.restoreGraphicsState;

  const png = rep.representationUsingTypeProperties($.NSBitmapImageFileTypePNG, $());
  if (!png.writeToFileAtomically(outPath, true)) throw new Error('Could not write ' + outPath);
}
