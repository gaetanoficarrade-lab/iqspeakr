#!/usr/bin/env osascript -l JavaScript
// IQspeakr Installer — Native macOS Fortschrittsfenster (JXA/Cocoa)
// Liest Status aus einer Datei und zeigt Fortschrittsbalken + Text.

ObjC.import('Cocoa');
ObjC.import('Foundation');

function run(argv) {
    const statusFile = argv[0] || '/tmp/iqspeakr-install-status';

    const app = $.NSApplication.sharedApplication;
    app.setActivationPolicy($.NSApplicationActivationPolicyRegular);

    // --- Fenster ---
    const win = $.NSWindow.alloc.initWithContentRectStyleMaskBackingDefer(
        $.NSMakeRect(0, 0, 480, 160),
        $.NSWindowStyleMaskTitled,
        $.NSBackingStoreBuffered,
        false
    );
    win.setTitle($('IQspeakr Installer'));
    win.center;
    win.setLevel($.NSFloatingWindowLevel);

    const content = win.contentView;

    // --- Titel-Label ---
    const titleLabel = $.NSTextField.alloc.initWithFrame($.NSMakeRect(25, 110, 430, 25));
    titleLabel.setStringValue($('IQspeakr wird installiert...'));
    titleLabel.setBezeled(false);
    titleLabel.setEditable(false);
    titleLabel.setDrawsBackground(false);
    titleLabel.setFont($.NSFont.boldSystemFontOfSize(14));
    content.addSubview(titleLabel);

    // --- Detail-Label ---
    const detailLabel = $.NSTextField.alloc.initWithFrame($.NSMakeRect(25, 85, 430, 20));
    detailLabel.setStringValue($('Bitte warten...'));
    detailLabel.setBezeled(false);
    detailLabel.setEditable(false);
    detailLabel.setDrawsBackground(false);
    detailLabel.setFont($.NSFont.systemFontOfSize(12));
    detailLabel.setTextColor($.NSColor.secondaryLabelColor);
    content.addSubview(detailLabel);

    // --- Fortschrittsbalken ---
    const progress = $.NSProgressIndicator.alloc.initWithFrame($.NSMakeRect(25, 50, 430, 20));
    progress.setIndeterminate(false);
    progress.setMinValue(0);
    progress.setMaxValue(100);
    progress.setDoubleValue(0);
    content.addSubview(progress);

    // --- Schritt-Label ---
    const stepLabel = $.NSTextField.alloc.initWithFrame($.NSMakeRect(25, 25, 430, 18));
    stepLabel.setStringValue($(''));
    stepLabel.setBezeled(false);
    stepLabel.setEditable(false);
    stepLabel.setDrawsBackground(false);
    stepLabel.setFont($.NSFont.systemFontOfSize(11));
    stepLabel.setTextColor($.NSColor.tertiaryLabelColor);
    content.addSubview(stepLabel);

    win.makeKeyAndOrderFront(null);
    app.activateIgnoringOtherApps(true);

    // --- Timer: Status-Datei pollen ---
    $.NSTimer.scheduledTimerWithTimeIntervalRepeatsBlock(0.3, true, function(timer) {
        try {
            const fm = $.NSFileManager.defaultManager;
            const path = $(statusFile);
            if (!fm.fileExistsAtPath(path)) return;

            const raw = $.NSString.stringWithContentsOfFileEncodingError(path, $.NSUTF8StringEncoding, null);
            if (!raw) return;
            const lines = raw.js.trim().split('\n');

            // Zeile 1: Prozent (0-100) oder DONE/ERROR
            const first = lines[0] || '';
            if (first === 'DONE') {
                timer.invalidate();
                progress.setDoubleValue(100);
                titleLabel.setStringValue($('Installation abgeschlossen!'));
                detailLabel.setStringValue($('IQspeakr ist bereit.'));
                stepLabel.setStringValue($(''));
                // Fenster nach 2 Sekunden schließen
                $.NSTimer.scheduledTimerWithTimeIntervalRepeatsBlock(2.0, false, function() {
                    app.terminate(null);
                });
                return;
            }
            if (first === 'ERROR') {
                timer.invalidate();
                titleLabel.setStringValue($('Fehler bei der Installation'));
                detailLabel.setStringValue($(lines[1] || 'Unbekannter Fehler'));
                stepLabel.setStringValue($(''));
                progress.setDoubleValue(0);
                return;
            }

            const pct = parseInt(first) || 0;
            progress.setDoubleValue(pct);

            // Zeile 2: Haupttext
            if (lines.length >= 2) {
                titleLabel.setStringValue($(lines[1]));
            }
            // Zeile 3: Detailtext
            if (lines.length >= 3) {
                detailLabel.setStringValue($(lines[2]));
            }
            // Zeile 4: Schrittanzeige
            if (lines.length >= 4) {
                stepLabel.setStringValue($(lines[3]));
            }
        } catch(e) {}
    });

    app.run;
}
