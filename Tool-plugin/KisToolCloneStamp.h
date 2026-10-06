/*
 *  SPDX-FileCopyrightText: 2026 metamountain <mail@metamountain.net>
 *  SPDX-License-Identifier: GPL-2.0-or-later
 */

#ifndef KIS_TOOL_CLONE_STAMP_H_
#define KIS_TOOL_CLONE_STAMP_H_

#include "KoToolFactoryBase.h"
#include "kis_tool.h"
#include "kis_types.h"
#include <kis_icon.h>
#include <KoIcon.h>
#include <KoResource.h>
#include <klocalizedstring.h>
#include <QPointF>
#include <QPoint>
#include <QRect>
#include <QImage>
#include <QScopedPointer>
#include <QTimer>
#include <QElapsedTimer>
#include <QHash>
#include <QVector>

class KisTransaction;
class KisSliderSpinBox;
class KisResourceItemChooser;
class QComboBox;
class QPainterPath;

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

    enum class TipShape {
        Round,
        Square,
        Bitmap // any Krita brush tip resource (gbr/png/abr/generated)
    };

    enum Preset {
        PresetRoundHard,
        PresetRoundSoft,
        PresetSquare,
        PresetPainterly,
        PresetAirbrush
    };

    // --- source -----------------------------------------------------------
    bool isValidPaintLayer(KisNodeSP node) const;
    bool canPaintOn(KisNodeSP node) const;
    void showMessage(const QString &text) const;
    void sampleSource(const QPointF &pixelPoint);
    void takeSourceSnapshot();
    // Current Layer reads m_sourceNode's own device (the layer active at
    // Ctrl+click time); All Layers reads the image's merged projection.
    KisPaintDeviceSP sourceDeviceForSampling() const;
    // Display copy of the snapshot (8-bit sRGB, premultiplied) -- used only
    // for the cursor preview, painting never goes through QImage.
    QImage readSourceImage(const QRect &rect) const;

    // --- brush tip --------------------------------------------------------
    // Coverage mask of one dab, white with the coverage in alpha, centered
    // in a square image. `diameter` is the tip's longer side in pixels.
    QImage buildDabMask(qreal diameter, qreal angleDeg, qreal alpha) const;
    // Cached variant for the common case (no per-dab angle jitter).
    const QImage &dabMask(qreal diameter, qreal angleDeg, qreal alpha) const;
    // Tip outline (destination/source rings) in pixel coordinates.
    QPainterPath tipOutline(const QPointF &center, qreal diameter) const;
    void setTipResource(KoResourceSP resource);
    void applyPreset(Preset preset);
    void syncOptionWidgets();

    // --- stroke -----------------------------------------------------------
    void beginStroke(const QPointF &pixelPoint);
    void stampDab(const QPointF &dstCenterPixels, qreal pressure);
    void strokeTo(const QPointF &pixelPoint, qreal pressure);
    void airbrushTick();
    void finalizeStroke();
    void compositeLive(const QRect &dstRect);
    // Composites everything stamped since the last flush in one pass.
    void flushComposite();

    // --- cursor/preview -----------------------------------------------------
    void updateOutline(const QPointF &pixelPoint);
    QImage buildPreviewPatch(const QPointF &srcCenterPixels) const;
    QImage cachedPreviewPatch(const QPointF &srcCenterPixels) const;
    qreal maskSide(qreal diameter) const;

    QWidget *m_optionWidget {nullptr};
    SampleScope m_sampleScope {SampleScope::CurrentLayer};

    // Clone source (set by Ctrl+click).
    KisNodeSP m_sourceNode;
    QPointF m_sourcePoint;
    bool m_hasSource {false};
    // Copy-on-write copy of the sampled device (layer or merged projection)
    // taken at Ctrl+click, in its own color space. Strokes that cross their
    // own source therefore clone the original pixels, like Photoshop.
    KisPaintDeviceSP m_sourceSnapshot;

    // Stroke coverage, 1 byte/px (Format_Alpha8, canvas-sized). Each dab
    // adds its flow-scaled tip mask here with SourceOver; compositing applies
    // the opacity on top, so flow builds up within a stroke while opacity
    // caps the whole stroke (Photoshop semantics).
    QImage m_accImage;
    KisSelectionSP m_strokeMask;    // reused per stroke, fed from m_accImage
    // Dabs are composited at most once per frame: stampDab only grows this
    // rect, flushComposite renders it (overlapping dabs computed once).
    QRect m_pendingRect;
    QElapsedTimer m_flushClock;
    QTimer m_flushTimer;
    int m_accLeft {0};
    int m_accTop {0};
    bool m_useAccumulator {false};
    KisPaintDeviceSP m_dstOriginal; // copy-on-write copy of the layer at stroke start

    // Brush tip settings.
    TipShape m_tipShape {TipShape::Round};
    int m_brushSize {250};          // px, longer side of the tip
    qreal m_brushHardness {0.5};    // Round/Square only
    int m_brushOpacity {100};       // %, ceiling of one stroke
    int m_flow {100};               // %, per dab
    int m_angle {0};                // degrees
    int m_roundness {100};          // %, short/long axis ratio
    int m_spacing {10};             // % of the dab size
    bool m_randomAngle {false};
    bool m_airbrush {false};
    int m_airbrushRate {20};        // dabs per second while holding still
    bool m_pressureSize {false};
    bool m_pressureFlow {false};
    QImage m_tipCoverage;           // Bitmap tip: white, coverage in alpha
    QString m_tipName;

    // Dab masks by parameters (incl. 5-degree angle steps for random angle).
    mutable QHash<QString, QImage> m_dabCache;
    mutable QImage m_squareCache;   // unrotated soft square, see buildDabMask
    mutable QString m_squareCacheKey;

    // Preview under the cursor.
    mutable QImage m_previewCache;
    mutable QString m_previewCacheKey;
    // Display-converted block of the source around the preview position;
    // moving inside it only crops, no color conversion per mouse move.
    mutable QImage m_previewSrcImage;
    mutable QRect m_previewSrcRect;
    int m_previewOpacity {85};      // %
    bool m_previewAutoHide {true};  // hide the overlay while a stroke is in progress

    // Aligned (GIMP/Photoshop semantics): the source-to-destination offset is
    // fixed after the first stroke and reused by every later stroke; when
    // false, each new stroke resamples from the original source point.
    bool m_aligned {true};
    // Photoshop habit: Alt+click sets the source too. Plain Alt+left click
    // is unbound in Krita's default canvas input profile, so it reaches
    // beginPrimaryAction with the Alt modifier set.
    bool m_altSamples {true};
    bool m_altSampling {false};     // current press was an Alt+click sample
    QPointF m_strokeOffset;
    bool m_hasStrokeOffset {false};

    QPointF m_lastDabPoint;
    qreal m_lastDabPressure {1.0};
    bool m_hasLastDabPoint {false};
    QPointF m_lastCursorPoint;
    qreal m_lastCursorPressure {1.0};
    QTimer m_airbrushTimer;

    bool m_isPainting {false};
    QScopedPointer<KisTransaction> m_transaction;

    // Shift+drag: horizontal = size, vertical = hardness (Photoshop-style).
    bool m_isResizing {false};
    QPoint m_resizeStartWidgetPos;
    int m_resizeStartSize {250};
    int m_resizeStartHardnessPercent {50};

    // Option widgets kept for syncing after presets / Shift+drag.
    QComboBox *m_shapeCombo {nullptr};
    KisSliderSpinBox *m_sizeSlider {nullptr};
    KisSliderSpinBox *m_hardnessSlider {nullptr};
    KisSliderSpinBox *m_opacitySlider {nullptr};
    KisSliderSpinBox *m_flowSlider {nullptr};
    KisSliderSpinBox *m_angleSlider {nullptr};
    KisSliderSpinBox *m_roundnessSlider {nullptr};
    KisSliderSpinBox *m_spacingSlider {nullptr};
    KisSliderSpinBox *m_rateSlider {nullptr};
    class QCheckBox *m_randomAngleCheck {nullptr};
    class QCheckBox *m_airbrushCheck {nullptr};
    KisResourceItemChooser *m_tipChooser {nullptr};

    QPointF m_hoverPoint;
    bool m_hasHoverPoint {false};
    QVector<QRectF> m_lastOutlineDocRects; // overlay areas painted last time

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
        setIconName(koIconNameCStr("krita_tool_clonestamp"));
        setActivationShapeId(KRITA_TOOL_ACTIVATION_ID);
    }

    ~KisToolCloneStampFactory() override {}

    KoToolBase *createTool(KoCanvasBase *canvas) override
    {
        return new KisToolCloneStamp(canvas);
    }
};

#endif // KIS_TOOL_CLONE_STAMP_H_
