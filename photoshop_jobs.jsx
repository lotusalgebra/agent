// LOTUS Photoshop job runner.
// Driven by photoshop.py — placeholder tokens (<<NAME>>) are replaced at
// runtime before this script is fed to `osascript -e 'do javascript file ...'`.
//
// Operations (run in this order on a single doc):
//   1. Open IMAGE_PATH.
//   2. For each REMOVAL_REGIONS [x,y,w,h]: select that rectangle and run
//      Content-Aware Fill via the Action Manager (no UI prompts, fully
//      scriptable, available in PS 2020+).
//   3. If WATERMARK_PATH is non-empty, place the watermark file as a new
//      layer, scale to WM_SCALE * doc.width, position top-left at WM_X,WM_Y,
//      and flatten.
//   4. Save as PNG to OUTPUT_PATH (overwrites if exists).

#target photoshop

var IMAGE_PATH    = "<<IMAGE_PATH>>";
var OUTPUT_PATH   = "<<OUTPUT_PATH>>";
var REMOVAL_REGIONS = <<REGIONS>>;
var WATERMARK_PATH = "<<WATERMARK_PATH>>";
var WM_X     = <<WM_X>>;
var WM_Y     = <<WM_Y>>;
var WM_SCALE = <<WM_SCALE>>;

function contentAwareFill() {
    var idfill = stringIDToTypeID("fill");
    var desc = new ActionDescriptor();
    desc.putEnumerated(
        stringIDToTypeID("using"),
        stringIDToTypeID("fillContents"),
        stringIDToTypeID("contentAware")
    );
    executeAction(idfill, desc, DialogModes.NO);
}

function selectRect(doc, x, y, w, h) {
    var x2 = x + w;
    var y2 = y + h;
    doc.selection.select([
        [x,  y ],
        [x2, y ],
        [x2, y2],
        [x,  y2]
    ]);
}

try {
    app.preferences.rulerUnits = Units.PIXELS;
    app.preferences.typeUnits  = TypeUnits.PIXELS;
    app.displayDialogs = DialogModes.NO;

    var doc = app.open(File(IMAGE_PATH));

    // ── 1. Removal passes ────────────────────────────────────────────
    for (var i = 0; i < REMOVAL_REGIONS.length; i++) {
        var r = REMOVAL_REGIONS[i];
        try {
            selectRect(doc, r[0], r[1], r[2], r[3]);
            contentAwareFill();
            doc.selection.deselect();
        } catch (e) {
            // Per-region failure shouldn't abort the whole job — log and move on.
            $.writeln("region " + i + " failed: " + e);
        }
    }

    // ── 2. Watermark placement ───────────────────────────────────────
    if (WATERMARK_PATH && WATERMARK_PATH.length > 0) {
        try {
            var wm = app.open(File(WATERMARK_PATH));
            wm.selection.selectAll();
            wm.selection.copy();
            wm.close(SaveOptions.DONOTSAVECHANGES);

            app.activeDocument = doc;
            doc.paste();
            var wmLayer = doc.activeLayer;

            // Scale: watermark width should be WM_SCALE * doc.width
            var targetWidth  = doc.width.value * WM_SCALE;
            var currentWidth = wmLayer.bounds[2].value - wmLayer.bounds[0].value;
            if (currentWidth > 0) {
                var pct = (targetWidth / currentWidth) * 100;
                wmLayer.resize(pct, pct, AnchorPosition.MIDDLECENTER);
            }

            // Position: move so layer's top-left is at (WM_X, WM_Y).
            var b = wmLayer.bounds;
            var dx = WM_X - b[0].value;
            var dy = WM_Y - b[1].value;
            wmLayer.translate(dx, dy);

            doc.flatten();
        } catch (we) {
            $.writeln("watermark place failed: " + we);
        }
    }

    // ── 3. Save as PNG ───────────────────────────────────────────────
    var saveOpts = new PNGSaveOptions();
    saveOpts.compression = 6;
    doc.saveAs(File(OUTPUT_PATH), saveOpts, true, Extension.LOWERCASE);
    doc.close(SaveOptions.DONOTSAVECHANGES);
    "OK:" + OUTPUT_PATH;
} catch (err) {
    "ERR:" + err.toString();
}
