/*
 *  SPDX-FileCopyrightText: 2026 metamountain <mail@metamountain.net>
 *  SPDX-License-Identifier: GPL-2.0-or-later
 */

#include "KisToolCloneStamp.h"

#include <QByteArray>
#include <QCheckBox>
#include <QColor>
#include <QComboBox>
#include <QGridLayout>
#include <QHBoxLayout>
#include <QLabel>
#include <QPainter>
#include <QPainterPath>
#include <QPen>
#include <QRadialGradient>
#include <QRandomGenerator>
#include <QRect>
#include <QSignalBlocker>
#include <QToolButton>
#include <QTransform>
#include <QVBoxLayout>
#include <QWidget>
#include <cmath>
#include <cstring>
#include <functional>

#include <KoCanvasBase.h>
#include <KoColorSpace.h>
#include <KoCompositeOpRegistry.h>
#include <KoPointerEvent.h>
#include <KoViewConverter.h>
#include <KisOptimizedBrushOutline.h>
#include <KisResourceItemChooser.h>
#include <KisResourceModel.h>
#include <KisResourceTypes.h>
#include <KisViewManager.h>
#include <kis_brush.h>
#include <kis_canvas2.h>
#include <kis_cursor.h>
#include <kis_image.h>
#include <kis_node.h>
#include <kis_paint_device.h>
#include <kis_painter.h>
#include <kis_pixel_selection.h>
#include <kis_selection.h>
#include <kis_slider_spin_box.h>
#include <kis_transaction.h>

namespace
{

// Ceiling on the per-stroke coverage buffer (pixel count) to bound memory:
// it is 4 bytes/px, so this caps it around 800MB.
constexpr qint64 MAX_ACCUMULATOR_PIXELS = 200000000; // ~14000x14000

// Coverage falloff shared by the round and square tips: solid up to
// `hardness` (fraction of the radius), then a smooth fade to 0 at the rim.
inline qreal falloff(qreal t, qreal hardness)
{
    if (t <= hardness) {
        return 1.0;
    }
    if (t >= 1.0) {
        return 0.0;
    }
    const qreal x = 1.0 - (t - hardness) / qMax(qreal(1e-6), 1.0 - hardness);
    return x * x * (3.0 - 2.0 * x); // smoothstep
}

// Bitmap tip -> white image with the coverage in alpha. Krita's convention
// for mask tips: dark = paint, combined with the image's own alpha
// (libs/brush/kis_brush.cpp); color image tips contribute their alpha only.
QImage tipCoverageFromBrush(const KisBrushSP &brush)
{
    const QImage src = brush->brushTipImage().convertToFormat(QImage::Format_ARGB32);
    if (src.isNull()) {
        return QImage();
    }
    const bool maskTip = brush->brushType() == MASK || brush->brushType() == PIPE_MASK;
    QImage out(src.size(), QImage::Format_ARGB32_Premultiplied);
    for (int y = 0; y < src.height(); ++y) {
        const QRgb *in = reinterpret_cast<const QRgb *>(src.constScanLine(y));
        QRgb *o = reinterpret_cast<QRgb *>(out.scanLine(y));
        for (int x = 0; x < src.width(); ++x) {
            const int a = maskTip ? (255 - qGray(in[x])) * qAlpha(in[x]) / 255 : qAlpha(in[x]);
            o[x] = qRgba(a, a, a, a); // premultiplied white
        }
    }
    return out;
}

} // namespace

KisToolCloneStamp::KisToolCloneStamp(KoCanvasBase *canvas)
    : KisTool(canvas, KisCursor::crossCursor())
{
    setObjectName("tool_clonestamp");
    connect(&m_airbrushTimer, &QTimer::timeout, this, [this]() { airbrushTick(); });
    m_flushTimer.setSingleShot(true);
    connect(&m_flushTimer, &QTimer::timeout, this, [this]() { flushComposite(); });
}

KisToolCloneStamp::~KisToolCloneStamp()
{
}

void KisToolCloneStamp::activate(const QSet<KoShape *> &shapes)
{
    KisTool::activate(shapes);
}

void KisToolCloneStamp::deactivate()
{
    m_airbrushTimer.stop();
    if (m_isPainting) {
        // A stroke was left open (e.g. tool switched mid-drag): it is already
        // on the layer, just close it properly.
        flushComposite();
        finalizeStroke();
    }
    if (m_transaction) {
        m_transaction->commit(image()->undoAdapter());
        m_transaction.reset();
    }
    m_isPainting = false;
    m_isResizing = false;
    KisTool::deactivate();
}

// ---------------------------------------------------------------- source

bool KisToolCloneStamp::isValidPaintLayer(KisNodeSP node) const
{
    // Any raster paint layer works, in any color model or bit depth: all
    // reading and writing goes through KisPainter / KisPaintDevice, which
    // handle the pixel format (and convert between color spaces).
    return node && node->inherits("KisPaintLayer") && node->paintDevice();
}

bool KisToolCloneStamp::canPaintOn(KisNodeSP node) const
{
    return isValidPaintLayer(node) && node->isEditable();
}

void KisToolCloneStamp::showMessage(const QString &text) const
{
    KisCanvas2 *kisCanvas = qobject_cast<KisCanvas2 *>(canvas());
    if (kisCanvas && kisCanvas->viewManager()) {
        kisCanvas->viewManager()->showFloatingMessage(text, QIcon());
    }
}

KisPaintDeviceSP KisToolCloneStamp::sourceDeviceForSampling() const
{
    if (!image()) {
        return nullptr;
    }
    if (m_sampleScope == SampleScope::AllLayers) {
        return image()->projection();
    }
    if (!isValidPaintLayer(m_sourceNode)) {
        return nullptr;
    }
    return m_sourceNode->paintDevice();
}

void KisToolCloneStamp::sampleSource(const QPointF &docPoint)
{
    KisNodeSP node = currentNode();
    if (m_sampleScope == SampleScope::CurrentLayer && !isValidPaintLayer(node)) {
        showMessage(i18n("Clone Stamp: select a paint layer to sample from, "
                         "or set Sample to \"All Layers\"."));
        return;
    }
    m_sourceNode = node;
    m_sourcePoint = docPoint;
    m_hasStrokeOffset = false;
    m_hasLastDabPoint = false;
    takeSourceSnapshot();
    m_hasSource = bool(m_sourceSnapshot);
}

void KisToolCloneStamp::takeSourceSnapshot()
{
    // Copy-on-write: this copies tile references, not pixels, so it is cheap
    // even for huge documents; tiles are duplicated only when the original
    // changes afterwards.
    m_sourceSnapshot = nullptr;
    KisPaintDeviceSP srcDevice = sourceDeviceForSampling();
    if (srcDevice) {
        m_sourceSnapshot = new KisPaintDevice(*srcDevice);
    }
    m_previewCache = QImage();
    m_previewSrcImage = QImage();
    m_previewSrcRect = QRect();
}

QImage KisToolCloneStamp::readSourceImage(const QRect &rect) const
{
    if (!m_sourceSnapshot || rect.isEmpty()) {
        return QImage();
    }
    const QImage img = m_sourceSnapshot->convertToQImage(nullptr, rect.x(), rect.y(), rect.width(), rect.height());
    return img.convertToFormat(QImage::Format_ARGB32_Premultiplied);
}

// ---------------------------------------------------------------- brush tip

qreal KisToolCloneStamp::maskSide(qreal diameter) const
{
    // Round tips stay inside their circle at any angle; square and bitmap
    // tips need room for their rotated corners.
    return m_tipShape == TipShape::Round ? diameter : diameter * M_SQRT2;
}

QImage KisToolCloneStamp::buildDabMask(qreal diameter, qreal angleDeg, qreal alpha) const
{
    diameter = qMax(qreal(1.0), diameter);
    const int side = qMax(1, int(std::ceil(maskSide(diameter))) + 2);
    QImage mask(side, side, QImage::Format_ARGB32_Premultiplied);
    mask.fill(Qt::transparent);

    const qreal roundness = qBound(1, m_roundness, 100) / 100.0;
    const qreal hardness = qBound(qreal(0.0), m_brushHardness, qreal(1.0));

    QPainter p(&mask);
    p.setRenderHint(QPainter::Antialiasing, true);
    p.setRenderHint(QPainter::SmoothPixmapTransform, true);
    p.translate(side / 2.0, side / 2.0);
    p.rotate(angleDeg);
    p.scale(1.0, roundness);
    p.setOpacity(qBound(qreal(0.0), alpha, qreal(1.0)));

    if (m_tipShape == TipShape::Round) {
        const qreal r = diameter / 2.0;
        QRadialGradient grad(QPointF(0, 0), qMax(r, qreal(0.5)));
        grad.setColorAt(0.0, Qt::white);
        grad.setColorAt(qMin(hardness, qreal(0.999)), Qt::white);
        // a few intermediate stops approximate the smoothstep falloff
        for (int i = 1; i < 4; ++i) {
            const qreal t = hardness + (1.0 - hardness) * i / 4.0;
            if (t > hardness + 0.001 && t < 0.999) {
                grad.setColorAt(t, QColor(255, 255, 255, qRound(255 * falloff(t, hardness))));
            }
        }
        grad.setColorAt(1.0, QColor(255, 255, 255, 0));
        p.setPen(Qt::NoPen);
        p.setBrush(grad);
        p.drawEllipse(QPointF(0, 0), r, r);
    } else if (m_tipShape == TipShape::Square) {
        // Unrotated soft square at the exact size, cached; Chebyshev distance
        // gives straight feathered edges.
        const int n = qMax(1, qRound(diameter));
        const QString key = QString("%1/%2").arg(n).arg(hardness);
        if (m_squareCacheKey != key) {
            QImage sq(n, n, QImage::Format_ARGB32_Premultiplied);
            const qreal c = (n - 1) / 2.0;
            const qreal half = qMax(qreal(0.5), n / 2.0);
            for (int y = 0; y < n; ++y) {
                QRgb *line = reinterpret_cast<QRgb *>(sq.scanLine(y));
                const qreal v = std::abs(y - c) / half;
                for (int x = 0; x < n; ++x) {
                    const qreal u = std::abs(x - c) / half;
                    const int a = qRound(255 * falloff(qMax(u, v), hardness));
                    line[x] = qRgba(a, a, a, a);
                }
            }
            m_squareCache = sq;
            m_squareCacheKey = key;
        }
        p.drawImage(QRectF(-diameter / 2.0, -diameter / 2.0, diameter, diameter), m_squareCache);
    } else if (!m_tipCoverage.isNull()) {
        const qreal scale = diameter / qMax(m_tipCoverage.width(), m_tipCoverage.height());
        const QSizeF s(m_tipCoverage.width() * scale, m_tipCoverage.height() * scale);
        p.drawImage(QRectF(QPointF(-s.width() / 2.0, -s.height() / 2.0), s), m_tipCoverage);
    }
    p.end();
    return mask;
}

const QImage &KisToolCloneStamp::dabMask(qreal diameter, qreal angleDeg, qreal alpha) const
{
    const QString key = QString("%1/%2/%3/%4/%5/%6/%7/%8")
                            .arg(int(m_tipShape))
                            .arg(qRound(diameter * 4))
                            .arg(m_brushHardness)
                            .arg(qRound(angleDeg))
                            .arg(m_roundness)
                            .arg(qRound(alpha * 255))
                            .arg(m_tipCoverage.cacheKey())
                            .arg(m_tipName);
    auto it = m_dabCache.find(key);
    if (it == m_dabCache.end()) {
        if (m_dabCache.size() > 96) {
            m_dabCache.clear(); // parameters changed; keep memory bounded
        }
        it = m_dabCache.insert(key, buildDabMask(diameter, angleDeg, alpha));
    }
    return it.value();
}

QPainterPath KisToolCloneStamp::tipOutline(const QPointF &center, qreal diameter) const
{
    QPainterPath base;
    const qreal r = diameter / 2.0;
    if (m_tipShape == TipShape::Round) {
        base.addEllipse(QPointF(0, 0), r, r);
    } else if (m_tipShape == TipShape::Square || m_tipCoverage.isNull()) {
        base.addRect(QRectF(-r, -r, diameter, diameter));
    } else {
        const qreal scale = diameter / qMax(m_tipCoverage.width(), m_tipCoverage.height());
        const qreal w = m_tipCoverage.width() * scale, h = m_tipCoverage.height() * scale;
        base.addRect(QRectF(-w / 2.0, -h / 2.0, w, h));
    }
    QTransform t;
    t.translate(center.x(), center.y());
    t.rotate(m_angle);
    t.scale(1.0, qBound(1, m_roundness, 100) / 100.0);
    return t.map(base);
}

void KisToolCloneStamp::setTipResource(KoResourceSP resource)
{
    KisBrushSP brush = resource.dynamicCast<KisBrush>();
    if (!brush) {
        return;
    }
    m_tipCoverage = tipCoverageFromBrush(brush);
    m_tipName = brush->name();
    m_previewCache = QImage();
    if (m_hasHoverPoint) {
        updateOutline(m_hoverPoint);
    }
}

void KisToolCloneStamp::applyPreset(Preset preset)
{
    m_angle = 0;
    m_roundness = 100;
    m_randomAngle = false;
    m_airbrush = false;
    m_flow = 100;
    m_spacing = 10;
    switch (preset) {
    case PresetRoundHard:
        m_tipShape = TipShape::Round;
        m_brushHardness = 1.0;
        break;
    case PresetRoundSoft:
        m_tipShape = TipShape::Round;
        m_brushHardness = 0.0;
        break;
    case PresetSquare:
        m_tipShape = TipShape::Square;
        m_brushHardness = 0.9;
        break;
    case PresetPainterly:
        m_tipShape = TipShape::Bitmap;
        m_flow = 60;
        m_spacing = 15;
        m_randomAngle = true;
        if (m_tipCoverage.isNull()) {
            // pick a textured tip from the installed brushes
            KisResourceModel model(ResourceType::Brushes);
            const QStringList wanted = {"chalk", "bristle", "charcoal", "texture", "rough"};
            for (const QString &w : wanted) {
                for (int row = 0; row < model.rowCount() && m_tipCoverage.isNull(); ++row) {
                    KoResourceSP res = model.resourceForIndex(model.index(row, 0));
                    if (res && res->name().contains(w, Qt::CaseInsensitive)) {
                        setTipResource(res);
                        if (m_tipChooser) {
                            m_tipChooser->setCurrentResource(res);
                        }
                    }
                }
            }
        }
        break;
    case PresetAirbrush:
        m_tipShape = TipShape::Round;
        m_brushHardness = 0.0;
        m_flow = 8;
        m_spacing = 5;
        m_airbrush = true;
        break;
    }
    syncOptionWidgets();
    if (m_hasHoverPoint) {
        updateOutline(m_hoverPoint);
    }
}

void KisToolCloneStamp::syncOptionWidgets()
{
    auto setSlider = [](KisSliderSpinBox *s, int v) {
        if (s) {
            QSignalBlocker b(s);
            s->setValue(v);
        }
    };
    if (m_shapeCombo) {
        QSignalBlocker b(m_shapeCombo);
        m_shapeCombo->setCurrentIndex(int(m_tipShape));
    }
    setSlider(m_sizeSlider, m_brushSize);
    setSlider(m_hardnessSlider, qRound(m_brushHardness * 100));
    setSlider(m_opacitySlider, m_brushOpacity);
    setSlider(m_flowSlider, m_flow);
    setSlider(m_angleSlider, m_angle);
    setSlider(m_roundnessSlider, m_roundness);
    setSlider(m_spacingSlider, m_spacing);
    setSlider(m_rateSlider, m_airbrushRate);
    if (m_randomAngleCheck) {
        QSignalBlocker b(m_randomAngleCheck);
        m_randomAngleCheck->setChecked(m_randomAngle);
    }
    if (m_airbrushCheck) {
        QSignalBlocker b(m_airbrushCheck);
        m_airbrushCheck->setChecked(m_airbrush);
    }
    if (m_hardnessSlider) {
        m_hardnessSlider->setEnabled(m_tipShape != TipShape::Bitmap);
    }
    if (m_tipChooser) {
        m_tipChooser->setVisible(m_tipShape == TipShape::Bitmap);
    }
    if (m_rateSlider) {
        m_rateSlider->setEnabled(m_airbrush);
    }
}

// ---------------------------------------------------------------- stroke

void KisToolCloneStamp::beginStroke(const QPointF &docPoint)
{
    if (!m_hasSource || !m_sourceSnapshot) {
        showMessage(i18n("Clone Stamp: Ctrl+click to set a source point first."));
        return;
    }
    KisNodeSP node = currentNode();
    if (!isValidPaintLayer(node)) {
        showMessage(i18n("Clone Stamp: select a paint layer to paint on."));
        return;
    }
    if (!node->isEditable()) {
        showMessage(i18n("Clone Stamp: the active layer is locked or hidden."));
        return;
    }
    if (m_tipShape == TipShape::Bitmap && m_tipCoverage.isNull()) {
        showMessage(i18n("Clone Stamp: choose a brush tip first."));
        return;
    }

    const QRect canvasBounds = image()->bounds();
    const qint64 pixels = qint64(canvasBounds.width()) * canvasBounds.height();
    if (pixels <= 0 || pixels > MAX_ACCUMULATOR_PIXELS) {
        showMessage(i18n("Clone Stamp: the image is too large."));
        return;
    }
    m_accImage = QImage(canvasBounds.width(), canvasBounds.height(), QImage::Format_Alpha8);
    if (m_accImage.isNull()) {
        showMessage(i18n("Clone Stamp: not enough memory for this stroke."));
        return;
    }
    m_accImage.fill(0);
    m_accLeft = canvasBounds.x();
    m_accTop = canvasBounds.y();
    m_useAccumulator = true;
    m_strokeMask = new KisSelection();
    m_pendingRect = QRect();
    m_flushClock.start();

    if (!(m_aligned && m_hasStrokeOffset)) {
        m_strokeOffset = QPointF(m_sourcePoint.x() - docPoint.x(), m_sourcePoint.y() - docPoint.y());
        m_hasStrokeOffset = true;
    }
    m_hasLastDabPoint = false;
    m_isPainting = true;

    KisPaintDeviceSP dst = node->paintDevice();
    m_dstOriginal = new KisPaintDevice(*dst); // copy-on-write, see header
    m_transaction.reset(new KisTransaction(dst));

    if (m_airbrush) {
        m_airbrushTimer.start(qMax(10, 1000 / qMax(1, m_airbrushRate)));
    }
}

void KisToolCloneStamp::stampDab(const QPointF &dstCenter, qreal pressure)
{
    if (!m_isPainting || !m_useAccumulator) {
        return;
    }
    const qreal diameter = qMax(qreal(1.0), m_brushSize * (m_pressureSize ? pressure : 1.0));
    const qreal alpha = m_flow / 100.0 * (m_pressureFlow ? pressure : 1.0);
    qreal angle = m_angle;
    if (m_randomAngle) {
        // 5-degree steps: 72 cached rotations instead of a new mask per dab
        angle += 5 * QRandomGenerator::global()->bounded(72);
    }
    const QImage *mask = &dabMask(diameter, angle, alpha);

    const QRect dabRect(qRound(dstCenter.x() - mask->width() / 2.0),
                        qRound(dstCenter.y() - mask->height() / 2.0),
                        mask->width(), mask->height());
    const QRect local = dabRect.translated(-m_accLeft, -m_accTop);
    const QRect clip = local.intersected(m_accImage.rect());
    if (clip.isEmpty()) {
        return;
    }
    QPainter painter(&m_accImage);
    painter.setCompositionMode(QPainter::CompositionMode_SourceOver);
    painter.drawImage(clip.topLeft(), *mask, clip.translated(-local.topLeft()));
    painter.end();

    m_pendingRect |= clip.translated(m_accLeft, m_accTop);
}

void KisToolCloneStamp::strokeTo(const QPointF &point, qreal pressure)
{
    // Dabs are laid out along the path at the spacing distance, so fast
    // mouse moves leave no gaps; pressure is interpolated between events.
    m_lastCursorPoint = point;
    m_lastCursorPressure = pressure;
    if (!m_hasLastDabPoint) {
        stampDab(point, pressure);
        m_lastDabPoint = point;
        m_lastDabPressure = pressure;
        m_hasLastDabPoint = true;
        return;
    }
    const qreal size = qMax(qreal(1.0), m_brushSize * (m_pressureSize ? pressure : 1.0));
    const qreal step = qMax(qreal(1.0), size * m_spacing / 100.0);
    QPointF from = m_lastDabPoint;
    qreal fromPressure = m_lastDabPressure;
    QPointF delta = point - from;
    qreal dist = std::hypot(delta.x(), delta.y());
    while (dist >= step) {
        const qreal t = step / dist;
        from += delta * t;
        fromPressure += (pressure - fromPressure) * t;
        stampDab(from, fromPressure);
        delta = point - from;
        dist = std::hypot(delta.x(), delta.y());
    }
    m_lastDabPoint = from;
    m_lastDabPressure = fromPressure;

    // Composite at most ~60 times per second; a pending timer catches the
    // last dabs when the pointer stops.
    const qint64 sinceFlush = m_flushClock.elapsed();
    if (sinceFlush >= 16) {
        flushComposite();
    } else if (!m_flushTimer.isActive()) {
        m_flushTimer.start(int(16 - sinceFlush));
    }
}

void KisToolCloneStamp::flushComposite()
{
    m_flushTimer.stop();
    if (m_pendingRect.isEmpty() || !m_isPainting) {
        m_pendingRect = QRect();
        return;
    }
    compositeLive(m_pendingRect);
    m_pendingRect = QRect();
    m_flushClock.restart();
}

void KisToolCloneStamp::airbrushTick()
{
    // Airbrush: keep depositing at the cursor while the button is held,
    // even without movement.
    if (m_isPainting && m_airbrush) {
        stampDab(m_lastCursorPoint, m_lastCursorPressure);
        flushComposite();
    }
}

void KisToolCloneStamp::compositeLive(const QRect &dstRectIn)
{
    // result = original OVER (source masked by stroke coverage x opacity),
    // recomputed for the area one dab touched and written straight to the
    // layer, in the layer's own pixel format.
    KisNodeSP dstNode = currentNode();
    if (!image() || !m_sourceSnapshot || !m_dstOriginal || !m_strokeMask || !canPaintOn(dstNode)) {
        return;
    }
    const QRect bounds = image()->bounds();
    const QPoint offset(qRound(m_strokeOffset.x()), qRound(m_strokeOffset.y()));
    // Only where both destination and source lie on the canvas.
    const QRect dstRect = dstRectIn & bounds & bounds.translated(-offset);
    if (dstRect.isEmpty()) {
        return;
    }
    KisPaintDeviceSP dst = dstNode->paintDevice();

    // 1. Restore the pre-stroke pixels of this area.
    KisPainter::copyAreaOptimized(dstRect.topLeft(), m_dstOriginal, dst, dstRect);

    // 2. Stroke coverage of this area into the stroke's selection mask
    //    (Alpha8 rows copied as-is).
    const QRect local = dstRect.translated(-m_accLeft, -m_accTop);
    QByteArray alpha(dstRect.width() * dstRect.height(), Qt::Uninitialized);
    for (int y = 0; y < local.height(); ++y) {
        memcpy(alpha.data() + y * local.width(), m_accImage.constScanLine(local.y() + y) + local.x(),
               size_t(local.width()));
    }
    m_strokeMask->pixelSelection()->writeBytes(reinterpret_cast<const quint8 *>(alpha.constData()), dstRect);

    // 3. Source over it through the mask, opacity applied by the painter
    //    (KisPainter converts color spaces).
    KisPainter gc(dst, m_strokeMask);
    gc.setCompositeOpId(COMPOSITE_OVER);
    gc.setOpacityF(qBound(0, m_brushOpacity, 100) / 100.0);
    gc.bitBlt(dstRect.topLeft(), m_sourceSnapshot, dstRect.translated(offset));
    gc.end();

    dst->setDirty(dstRect);
}

void KisToolCloneStamp::finalizeStroke()
{
    // Every dab was already composited onto the layer by compositeLive; all
    // that is left is dropping the per-stroke buffers.
    m_airbrushTimer.stop();
    m_flushTimer.stop();
    m_pendingRect = QRect();
    m_strokeMask = nullptr;
    m_accImage = QImage();
    m_useAccumulator = false;
    m_dstOriginal = nullptr;
}

// ---------------------------------------------------------------- cursor

void KisToolCloneStamp::updateOutline(const QPointF &pixelPoint)
{
    const QPointF moveVector = m_hasHoverPoint ? pixelPoint - m_hoverPoint : QPointF();
    m_hoverPoint = pixelPoint;
    m_hasHoverPoint = true;

    m_hasPreviewSource = m_hasSource;
    if (m_hasPreviewSource) {
        if (m_hasStrokeOffset) {
            m_previewSourcePoint = QPointF(pixelPoint.x() + m_strokeOffset.x(), pixelPoint.y() + m_strokeOffset.y());
        } else {
            m_previewSourcePoint = m_sourcePoint;
        }
    }

    KisCanvas2 *kisCanvas = qobject_cast<KisCanvas2 *>(canvas());
    if (!image() || !kisCanvas) {
        return;
    }

    // Same mechanism as KisToolPaint::requestUpdateOutline: overlay-only
    // updates through updateCanvasToolOutlineDoc, which repaints right away
    // instead of going through the compressed projection+overlay update that
    // updateCanvas() uses (that one made the cursor trail behind the mouse).
    // The destination and source rings are updated as two small rects, not
    // one box spanning both.
    const qreal half = maskSide(qMax(1, m_brushSize)) / 2.0;
    const qreal moveDistance = std::hypot(moveVector.x(), moveVector.y());
    auto ringDocRect = [&](const QPointF &center) {
        QRectF r(center.x() - half, center.y() - half, half * 2, half * 2);
        r.adjust(-4, -4, 4, 4);
        // Update-ahead (see KisToolPaint, bug 476300): also cover where the
        // ring will most likely be next, so a repaint arriving after the
        // following move doesn't tear the outline.
        if (moveDistance < 0.5 * qMax(r.width(), r.height())) {
            r |= r.translated(1.1 * moveVector);
        }
        return image()->pixelToDocument(r);
    };

    QVector<QRectF> rects;
    rects << ringDocRect(pixelPoint);
    if (m_hasPreviewSource) {
        rects << ringDocRect(m_previewSourcePoint);
    }
    for (const QRectF &r : std::as_const(m_lastOutlineDocRects)) {
        kisCanvas->updateCanvasToolOutlineDoc(r);
    }
    for (const QRectF &r : std::as_const(rects)) {
        kisCanvas->updateCanvasToolOutlineDoc(r);
    }
    m_lastOutlineDocRects = rects;
}

QImage KisToolCloneStamp::buildPreviewPatch(const QPointF &srcCenterPixels) const
{
    if (!image()) {
        return QImage();
    }
    // The preview shows the tip's shape (at full flow, without jitter).
    const QImage &mask = dabMask(qMax(1, m_brushSize), m_angle, 1.0);
    const QRect srcRect(qRound(srcCenterPixels.x() - mask.width() / 2.0),
                        qRound(srcCenterPixels.y() - mask.height() / 2.0),
                        mask.width(), mask.height());
    const QRect clip = srcRect.intersected(image()->bounds());
    if (clip.isEmpty()) {
        return QImage();
    }
    // Convert a block around the source once; moving inside it only crops.
    if (m_previewSrcImage.isNull() || !m_previewSrcRect.contains(clip)) {
        const int margin = qMax(32, mask.width() / 2);
        m_previewSrcRect = clip.adjusted(-margin, -margin, margin, margin).intersected(image()->bounds());
        m_previewSrcImage = readSourceImage(m_previewSrcRect);
    }
    const QImage clipImage = m_previewSrcImage.copy(clip.translated(-m_previewSrcRect.topLeft()));
    if (clipImage.isNull()) {
        return QImage();
    }
    QImage patch(mask.size(), QImage::Format_ARGB32_Premultiplied);
    patch.fill(Qt::transparent);
    QPainter painter(&patch);
    painter.drawImage(clip.x() - srcRect.x(), clip.y() - srcRect.y(), clipImage);
    painter.setCompositionMode(QPainter::CompositionMode_DestinationIn);
    painter.drawImage(0, 0, mask);
    painter.end();
    return patch;
}

QImage KisToolCloneStamp::cachedPreviewPatch(const QPointF &srcCenterPixels) const
{
    const QPoint srcPos = srcCenterPixels.toPoint();
    const QString key = QString("%1,%2/%3/%4/%5/%6/%7/%8")
                            .arg(srcPos.x()).arg(srcPos.y())
                            .arg(int(m_tipShape)).arg(m_brushSize).arg(m_brushHardness)
                            .arg(m_angle).arg(m_roundness).arg(m_tipCoverage.cacheKey());
    if (m_previewCache.isNull() || key != m_previewCacheKey) {
        m_previewCache = buildPreviewPatch(srcCenterPixels);
        m_previewCacheKey = key;
    }
    return m_previewCache;
}

void KisToolCloneStamp::paint(QPainter &gc, const KoViewConverter &converter)
{
    Q_UNUSED(converter);
    if (!m_hasHoverPoint || !image()) {
        return;
    }
    const qreal diameter = qMax(1, m_brushSize);

    if (m_hasPreviewSource && m_previewOpacity > 0 && !(m_isPainting && m_previewAutoHide)) {
        const QImage preview = cachedPreviewPatch(m_previewSourcePoint);
        if (!preview.isNull()) {
            const QRectF pixelRect(m_hoverPoint.x() - preview.width() / 2.0,
                                   m_hoverPoint.y() - preview.height() / 2.0,
                                   preview.width(), preview.height());
            gc.save();
            gc.setOpacity(m_previewOpacity / 100.0);
            gc.drawImage(pixelToView(pixelRect), preview);
            gc.restore();
        }
    }

    // Tip outlines and crosshairs go through Krita's own tool-outline
    // renderer (GPU-drawn on the OpenGL canvas, inverted against the image)
    // -- the same path and look as the regular brush outline.
    // 6 screen pixels expressed in image pixels at the current zoom.
    const qreal viewPerPixel = pixelToView(QPointF(1.0, 0.0)).x() - pixelToView(QPointF(0.0, 0.0)).x();
    const qreal crossPx = 6.0 / qMax(qreal(0.01), viewPerPixel);
    auto addTip = [&](QPainterPath &path, const QPointF &c) {
        path.addPath(tipOutline(c, diameter));
        path.moveTo(c.x() - crossPx, c.y());
        path.lineTo(c.x() + crossPx, c.y());
        path.moveTo(c.x(), c.y() - crossPx);
        path.lineTo(c.x(), c.y() + crossPx);
    };
    QPainterPath outline;
    addTip(outline, m_hoverPoint);
    if (m_hasPreviewSource) {
        addTip(outline, m_previewSourcePoint);
    }
    paintToolOutline(&gc, pixelToView(KisOptimizedBrushOutline(outline)));

    // Dashed inner outline marking the fully-hard zone of round/square tips.
    if (m_tipShape != TipShape::Bitmap && m_brushHardness < 0.99) {
        const QPainterPath hard = pixelToView(tipOutline(m_hoverPoint, qMax(qreal(3.0), diameter * m_brushHardness)));
        gc.save();
        gc.setBrush(Qt::NoBrush);
        gc.setPen(QPen(QColor(0, 0, 0, 160), 1, Qt::DashLine));
        gc.drawPath(hard.translated(1, 1));
        gc.setPen(QPen(QColor(255, 255, 255, 160), 1, Qt::DashLine));
        gc.drawPath(hard);
        gc.restore();
    }

    if (m_hasPreviewSource) {
        // Red accent on the source crosshair keeps source and destination
        // distinguishable at a glance.
        const QPointF center = pixelToView(m_previewSourcePoint);
        const qreal crossRadius = 6.0;
        gc.save();
        gc.setPen(QPen(QColor(255, 0, 0, 110), 2));
        gc.drawLine(QPointF(center.x() - crossRadius, center.y()), QPointF(center.x() + crossRadius, center.y()));
        gc.drawLine(QPointF(center.x(), center.y() - crossRadius), QPointF(center.x(), center.y() + crossRadius));
        gc.restore();
    }
}

// ---------------------------------------------------------------- input

void KisToolCloneStamp::beginPrimaryAction(KoPointerEvent *event)
{
    const QPointF pixelPoint = convertToPixelCoord(event);
    if (m_altSamples && (event->modifiers() & Qt::AltModifier)) {
        m_altSampling = true;
        sampleSource(pixelPoint);
        updateOutline(pixelPoint);
        return;
    }
    beginStroke(pixelPoint);
    if (m_isPainting) {
        strokeTo(pixelPoint, event->pressure());
        flushComposite(); // first dab shows at once
    }
    updateOutline(pixelPoint);
}

void KisToolCloneStamp::continuePrimaryAction(KoPointerEvent *event)
{
    if (m_altSampling || !m_isPainting) {
        return;
    }
    const QPointF pixelPoint = convertToPixelCoord(event);
    strokeTo(pixelPoint, event->pressure());
    updateOutline(pixelPoint);
}

void KisToolCloneStamp::endPrimaryAction(KoPointerEvent *event)
{
    Q_UNUSED(event);
    if (m_altSampling) {
        m_altSampling = false;
        return;
    }
    if (m_isPainting) {
        flushComposite();
        m_isPainting = false;
        m_hasLastDabPoint = false;
        finalizeStroke();
        if (m_transaction) {
            m_transaction->commit(image()->undoAdapter());
            m_transaction.reset();
        }
        if (m_hasHoverPoint) {
            updateOutline(m_hoverPoint); // bring the preview back (auto-hide)
        }
    }
}

void KisToolCloneStamp::mouseMoveEvent(KoPointerEvent *event)
{
    if (!m_isPainting && !m_isResizing) {
        updateOutline(convertToPixelCoord(event));
    }
    KisTool::mouseMoveEvent(event);
}

void KisToolCloneStamp::beginAlternateAction(KoPointerEvent *event, AlternateAction action)
{
    if (action == SampleFgImage) {
        const QPointF pixelPoint = convertToPixelCoord(event);
        sampleSource(pixelPoint);
        updateOutline(pixelPoint);
        return;
    }
    if (action == ChangeSize) {
        m_isResizing = true;
        m_resizeStartWidgetPos = event->pos();
        m_resizeStartSize = m_brushSize;
        m_resizeStartHardnessPercent = qRound(m_brushHardness * 100);
        return;
    }
    KisTool::beginAlternateAction(event, action);
}

void KisToolCloneStamp::continueAlternateAction(KoPointerEvent *event, AlternateAction action)
{
    if (action == ChangeSize && m_isResizing) {
        const int dx = event->pos().x() - m_resizeStartWidgetPos.x();
        // Screen y grows downward, so negate: dragging up increases
        // hardness, dragging down softens -- Photoshop's on-canvas convention.
        const int dy = event->pos().y() - m_resizeStartWidgetPos.y();
        m_brushSize = qBound(1, m_resizeStartSize + dx, 2000);
        m_brushHardness = qBound(0, m_resizeStartHardnessPercent - dy, 100) / 100.0;
        syncOptionWidgets();
        updateOutline(m_hoverPoint);
        return;
    }
    KisTool::continueAlternateAction(event, action);
}

void KisToolCloneStamp::endAlternateAction(KoPointerEvent *event, AlternateAction action)
{
    if (action == ChangeSize && m_isResizing) {
        m_isResizing = false;
        return;
    }
    KisTool::endAlternateAction(event, action);
}

// ---------------------------------------------------------------- options

QWidget *KisToolCloneStamp::createOptionWidget()
{
    if (m_optionWidget) {
        return m_optionWidget;
    }

    // Compact layout: everything fits the Tool Options docker without
    // scrolling -- sample source first, sliders in two columns.
    QWidget *widget = new QWidget();
    QVBoxLayout *layout = new QVBoxLayout(widget);
    layout->setContentsMargins(0, 0, 0, 0);
    layout->setSpacing(3);

    auto refresh = [this]() {
        if (m_hasHoverPoint) {
            updateOutline(m_hoverPoint);
        }
    };
    auto makeSlider = [&](const QString &prefix, const QString &suffix, int min, int max, int value,
                          const QString &tip, std::function<void(int)> apply) {
        KisSliderSpinBox *s = new KisSliderSpinBox();
        s->setRange(min, max);
        s->setPrefix(prefix);
        s->setSuffix(suffix);
        s->setValue(value);
        s->setToolTip(tip);
        s->setSizePolicy(QSizePolicy::Expanding, QSizePolicy::Fixed);
        connect(s, QOverload<int>::of(&KisSliderSpinBox::valueChanged), this, [apply, refresh](int v) {
            apply(v);
            refresh();
        });
        return s;
    };
    auto smallCheck = [&](const QString &text, bool checked, const QString &tip, std::function<void(bool)> apply) {
        QCheckBox *c = new QCheckBox(text);
        c->setChecked(checked);
        c->setToolTip(tip);
        connect(c, &QCheckBox::toggled, this, [apply](bool on) { apply(on); });
        return c;
    };

    // 1. Sample source (most used) + Aligned
    QHBoxLayout *sampleRow = new QHBoxLayout();
    sampleRow->setSpacing(2);
    const QString sampleTip = i18n("What Ctrl+click reads from: only the active layer, or the "
                                   "whole image as you see it (all layers merged). Takes effect "
                                   "at the next Ctrl+click.");
    QToolButton *curBtn = new QToolButton();
    QToolButton *allBtn = new QToolButton();
    curBtn->setText(i18n("Current Layer"));
    allBtn->setText(i18n("All Layers"));
    for (QToolButton *b : {curBtn, allBtn}) {
        b->setCheckable(true);
        b->setAutoExclusive(true);
        b->setToolTip(sampleTip);
        b->setSizePolicy(QSizePolicy::Expanding, QSizePolicy::Fixed);
        sampleRow->addWidget(b);
    }
    (m_sampleScope == SampleScope::AllLayers ? allBtn : curBtn)->setChecked(true);
    connect(allBtn, &QToolButton::toggled, this, [this](bool on) {
        m_sampleScope = on ? SampleScope::AllLayers : SampleScope::CurrentLayer;
    });
    sampleRow->addWidget(smallCheck(i18n("Aligned"), m_aligned,
                                    i18n("Checked: the source moves with your strokes -- the offset "
                                         "fixed by the first stroke is kept for all later strokes "
                                         "(Photoshop-style).\nUnchecked: every new stroke starts "
                                         "cloning from the original sampled point again."),
                                    [this](bool on) {
                                        m_aligned = on;
                                        // Non-Aligned resamples from the original point.
                                        if (!on) {
                                            m_hasStrokeOffset = false;
                                        }
                                    }));
    layout->addLayout(sampleRow);

    // 2. Presets, one row
    QHBoxLayout *presetRow = new QHBoxLayout();
    presetRow->setSpacing(2);
    const QList<QPair<QString, Preset>> presets = {
        {i18n("Hard"), PresetRoundHard}, {i18n("Soft"), PresetRoundSoft}, {i18n("Square"), PresetSquare},
        {i18n("Painterly"), PresetPainterly}, {i18n("Airbrush"), PresetAirbrush}};
    const QStringList presetTips = {i18n("Round, hard edge"), i18n("Round, soft edge"),
                                    i18n("Square, slightly feathered"),
                                    i18n("Textured brush tip, random angle, 60% flow"),
                                    i18n("Soft round, 8% flow, builds up while held")};
    for (int i = 0; i < presets.size(); ++i) {
        QToolButton *b = new QToolButton();
        b->setText(presets[i].first);
        b->setToolTip(presetTips[i]);
        b->setSizePolicy(QSizePolicy::Expanding, QSizePolicy::Fixed);
        const Preset preset = presets[i].second;
        connect(b, &QToolButton::clicked, this, [this, preset]() { applyPreset(preset); });
        presetRow->addWidget(b);
    }
    layout->addLayout(presetRow);

    // 3. Tip shape + (only for Brush Tip) Krita's tip chooser
    QHBoxLayout *shapeRow = new QHBoxLayout();
    shapeRow->setSpacing(4);
    shapeRow->addWidget(new QLabel(i18n("Tip:")));
    m_shapeCombo = new QComboBox();
    m_shapeCombo->addItem(i18n("Round"));
    m_shapeCombo->addItem(i18n("Square"));
    m_shapeCombo->addItem(i18n("Brush Tip"));
    m_shapeCombo->setToolTip(i18n("Round and Square are generated tips with hardness.\n"
                                  "Brush Tip uses any of Krita's built-in brush tips, including "
                                  "imported Photoshop .abr brushes."));
    connect(m_shapeCombo, QOverload<int>::of(&QComboBox::currentIndexChanged), this, [this, refresh](int i) {
        m_tipShape = TipShape(i);
        if (m_tipShape == TipShape::Bitmap && m_tipCoverage.isNull() && m_tipChooser) {
            setTipResource(m_tipChooser->currentResource());
        }
        syncOptionWidgets();
        refresh();
    });
    shapeRow->addWidget(m_shapeCombo, 1);
    layout->addLayout(shapeRow);

    m_tipChooser = new KisResourceItemChooser(ResourceType::Brushes, false);
    m_tipChooser->setRowHeight(32);
    m_tipChooser->setColumnWidth(32);
    m_tipChooser->showTaggingBar(true);
    m_tipChooser->setFixedHeight(150);
    connect(m_tipChooser, &KisResourceItemChooser::resourceSelected, this,
            [this](KoResourceSP res) { setTipResource(res); });
    layout->addWidget(m_tipChooser);

    // 4. Sliders, two columns
    QGridLayout *grid = new QGridLayout();
    grid->setHorizontalSpacing(3);
    grid->setVerticalSpacing(3);
    m_sizeSlider = makeSlider(i18n("Size: "), i18n(" px"), 1, 2000, m_brushSize,
                              i18n("Brush size in pixels.\nOn canvas: Shift+drag horizontally."),
                              [this](int v) { m_brushSize = v; });
    m_sizeSlider->setExponentRatio(3.0);
    m_hardnessSlider = makeSlider(i18n("Hard: "), i18n(" %"), 0, 100, qRound(m_brushHardness * 100),
                                  i18n("Edge hardness of Round/Square tips.\n"
                                       "On canvas: Shift+drag vertically (up = harder)."),
                                  [this](int v) { m_brushHardness = v / 100.0; });
    m_opacitySlider = makeSlider(i18n("Opacity: "), i18n(" %"), 0, 100, m_brushOpacity,
                                 i18n("Maximum coverage of one stroke -- overlapping dabs never "
                                      "build past this. Separate strokes do add up."),
                                 [this](int v) { m_brushOpacity = v; });
    m_flowSlider = makeSlider(i18n("Flow: "), i18n(" %"), 1, 100, m_flow,
                              i18n("Coverage each dab adds. Low flow builds up gradually "
                                   "within a stroke, up to the opacity."),
                              [this](int v) { m_flow = v; });
    m_angleSlider = makeSlider(i18n("Angle: "), i18n("°"), 0, 359, m_angle,
                               i18n("Rotation of the brush tip."),
                               [this](int v) { m_angle = v; });
    m_roundnessSlider = makeSlider(i18n("Round: "), i18n(" %"), 1, 100, m_roundness,
                                   i18n("Roundness: squash the tip into a flat/elliptical shape."),
                                   [this](int v) { m_roundness = v; });
    m_spacingSlider = makeSlider(i18n("Spacing: "), i18n(" %"), 1, 200, m_spacing,
                                 i18n("Distance between dabs, in percent of the brush size."),
                                 [this](int v) { m_spacing = v; });
    m_rateSlider = makeSlider(i18n("Rate: "), i18n(" /s"), 1, 100, m_airbrushRate,
                              i18n("Airbrush dabs per second while holding still."),
                              [this](int v) { m_airbrushRate = v; });
    KisSliderSpinBox *previewSlider = makeSlider(
        i18n("Preview: "), i18n(" %"), 0, 100, m_previewOpacity,
        i18n("Opacity of the source preview under the brush cursor. 0% turns it off."),
        [this](int v) { m_previewOpacity = v; });
    const QList<QWidget *> cells = {m_sizeSlider, m_hardnessSlider, m_opacitySlider, m_flowSlider,
                                    m_angleSlider, m_roundnessSlider, m_spacingSlider, m_rateSlider,
                                    previewSlider};
    for (int i = 0; i < cells.size(); ++i) {
        grid->addWidget(cells[i], i / 2, i % 2);
    }
    grid->addWidget(smallCheck(i18n("Auto-hide"), m_previewAutoHide,
                               i18n("Hide the source preview during a stroke, so you see the "
                                    "actual painted result under the cursor."),
                               [this](bool on) { m_previewAutoHide = on; }),
                    cells.size() / 2, 1);
    layout->addLayout(grid);

    // 5. Toggles, one row
    QHBoxLayout *toggleRow = new QHBoxLayout();
    toggleRow->setSpacing(6);
    m_randomAngleCheck = smallCheck(i18n("Rnd angle"), m_randomAngle,
                                    i18n("Rotate every dab randomly -- natural, painterly edges "
                                         "with textured tips."),
                                    [this](bool on) { m_randomAngle = on; });
    m_airbrushCheck = smallCheck(i18n("Airbrush"), m_airbrush,
                                 i18n("Keep building up while the button is held, even without "
                                      "moving (use with low Flow)."),
                                 [this](bool on) {
                                     m_airbrush = on;
                                     syncOptionWidgets();
                                 });
    toggleRow->addWidget(smallCheck(i18n("Alt-click"), m_altSamples,
                                    i18n("Alt+click also sets the source (Photoshop habit). "
                                         "Ctrl+click always works."),
                                    [this](bool on) { m_altSamples = on; }));
    toggleRow->addWidget(m_randomAngleCheck);
    toggleRow->addWidget(m_airbrushCheck);
    toggleRow->addWidget(new QLabel(i18n("Pressure:")));
    toggleRow->addWidget(smallCheck(i18n("Size"), m_pressureSize, i18n("Pen pressure controls the size."),
                                    [this](bool on) { m_pressureSize = on; }));
    toggleRow->addWidget(smallCheck(i18n("Flow"), m_pressureFlow, i18n("Pen pressure controls the flow."),
                                    [this](bool on) { m_pressureFlow = on; }));
    toggleRow->addStretch();
    layout->addLayout(toggleRow);

    layout->addStretch();
    m_optionWidget = widget;
    syncOptionWidgets();
    return m_optionWidget;
}
