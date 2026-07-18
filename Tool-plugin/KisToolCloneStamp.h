/*
 *  SPDX-FileCopyrightText: 2026 metamountain <mail@metamountain.net>
 *  SPDX-License-Identifier: GPL-2.0-or-later
 */

#ifndef KIS_TOOL_CLONESTAMP_H_
#define KIS_TOOL_CLONESTAMP_H_

#include "KoToolFactoryBase.h"
#include "kis_tool.h"
#include "kis_types.h"
#include <kis_icon.h>
#include <KoIcon.h>
#include <klocalizedstring.h>
#include <QPointF>
#include <QPoint>
#include <QScopedPointer>

class KisTransaction;
class QSpinBox;

class KisToolCloneStamp : public KisTool
{
    Q_OBJECT

public:
    explicit KisToolCloneStamp(KoCanvasBase *canvas);
    ~KisToolCloneStamp() override;

    void paint(QPainter &gc, const KoViewConverter &converter) override;

    void beginPrimaryAction(KoPointerEvent *event) override;
    void continuePrimaryAction(KoPointerEvent *event) override;
    void endPrimaryAction(KoPointerEvent *event) override;
    void mouseMoveEvent(KoPointerEvent *event) override;

    // Krita's input manager intercepts Ctrl+click and Shift+drag itself
    // (as the built-in "Sample Foreground Image" and "Change Size" alternate
    // actions) before they ever reach beginPrimaryAction with those
    // modifiers set -- so sampling and resize have to be implemented here,
    // not via checking event->modifiers() in the primary-action methods.
    void beginAlternateAction(KoPointerEvent *event, AlternateAction action) override;
    void continueAlternateAction(KoPointerEvent *event, AlternateAction action) override;
    void endAlternateAction(KoPointerEvent *event, AlternateAction action) override;

    QWidget *createOptionWidget() override;

protected:
    void activate(const QSet<KoShape *> &shapes) override;
    void deactivate() override;

private:
    enum class SampleScope {
        CurrentLayer,
        AllLayers
    };

    bool isValidPaintLayer(KisNodeSP node) const;
    void sampleSource(const QPointF &pixelPoint);
    void beginStroke(const QPointF &pixelPoint);
    void stampDabAt(const QPointF &dstCenterPixels);
    void updateOutline(const QPointF &pixelPoint);
    QImage buildPreviewPatch(const QPointF &srcCenterPixels) const;
    // Current Layer reads m_sourceNode's own device (the layer active at
    // Ctrl+click time); All Layers reads the image's merged projection --
    // Current & Below is deferred, it needs partial layer-stack compositing.
    KisPaintDeviceSP sourceDeviceForSampling() const;

    QWidget *m_optionWidget {nullptr};
    SampleScope m_sampleScope {SampleScope::CurrentLayer};

    // Clone source (set by Ctrl+click).
    KisNodeSP m_sourceNode;
    QPointF m_sourcePoint;
    bool m_hasSource {false};

    // Aligned (GIMP/Photoshop semantics): the source-to-destination offset is
    // fixed after the first stroke and reused by every later stroke; when
    // false, each new stroke resamples from the original source point.
    bool m_aligned {true};
    QPointF m_strokeOffset;
    bool m_hasStrokeOffset {false};

    QPointF m_lastDabPoint;
    bool m_hasLastDabPoint {false};

    int m_brushSize {250};
    qreal m_brushHardness {0.5};
    int m_brushOpacity {100};

    bool m_isPainting {false};
    QScopedPointer<KisTransaction> m_transaction;

    bool m_isResizing {false};
    QPoint m_resizeStartWidgetPos;
    int m_resizeStartSize {250};
    // Vertical component of the same Shift+drag gesture adjusts hardness
    // (drag up = harder, down = softer), mirroring Photoshop's on-canvas
    // brush resize where the two axes are independent.
    int m_resizeStartHardnessPercent {50};

    // Kept so a Shift+drag resize/hardness change can push its new values
    // back into the on-screen option widget, not just the internal state.
    QSpinBox *m_sizeSpin {nullptr};
    QSpinBox *m_hardnessSpin {nullptr};

    QPointF m_hoverPoint;
    bool m_hasHoverPoint {false};
    QRectF m_lastOutlineUpdateRect;

    // Where the source-side crosshair/preview should be drawn: hoverPoint +
    // strokeOffset once a stroke has fixed one, otherwise the raw sampled
    // point (so Ctrl+click gives immediate feedback before any painting).
    QPointF m_previewSourcePoint;
    bool m_hasPreviewSource {false};
};

class KisToolCloneStampFactory : public KoToolFactoryBase
{
public:
    KisToolCloneStampFactory()
        : KoToolFactoryBase("KritaShape/KisToolCloneStamp")
    {
        setToolTip(i18n("Clonestamp Tool with Preview"));
        setSection(ToolBoxSection::Fill);
        // 3 collided with tool_lazybrush's Colorize Mask Tool (also 3),
        // producing two icon slots that look identical if the icon is also
        // borrowed -- 5 is free between Smart Patch (4) and Fill (14).
        setPriority(5);
        setIconName(koIconNameCStr("edit-copy"));
        setActivationShapeId(KRITA_TOOL_ACTIVATION_ID);
    }

    ~KisToolCloneStampFactory() override {}

    KoToolBase *createTool(KoCanvasBase *canvas) override
    {
        return new KisToolCloneStamp(canvas);
    }
};

#endif // KIS_TOOL_CLONESTAMP_H_
