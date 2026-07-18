/*
 *  SPDX-FileCopyrightText: 2026 metamountain <mail@metamountain.net>
 *  SPDX-License-Identifier: GPL-2.0-or-later
 */

#include "KisToolCloneStamp.h"

#include <QPainter>
#include <QRadialGradient>
#include <QColor>
#include <QRect>
#include <QByteArray>
#include <QPen>
#include <QWidget>
#include <QVBoxLayout>
#include <QHBoxLayout>
#include <QLabel>
#include <QSpinBox>
#include <QCheckBox>
#include <QComboBox>
#include <QSignalBlocker>

#include <kis_cursor.h>
#include <KoPointerEvent.h>
#include <KoCanvasBase.h>
#include <KoViewConverter.h>

#include <kis_node.h>
#include <kis_paint_device.h>
#include <kis_image.h>
#include <kis_transaction.h>
#include <KoColorSpace.h>

namespace
{

// Ceiling on the per-stroke accumulator and the source snapshot (pixel
// count) to bound worst-case memory use -- a Format_ARGB32_Premultiplied
// buffer this size is 4 bytes/px, so this caps each around 800MB. Mirrors
// MAX_ACCUMULATOR_PIXELS / MAX_SOURCE_SNAPSHOT_PIXELS in the Python
// plugin's clonestamp_core.py; keep the two in sync.
constexpr qint64 MAX_ACCUMULATOR_PIXELS = 200000000; // ~14000x14000
constexpr qint64 MAX_SOURCE_SNAPSHOT_PIXELS = MAX_ACCUMULATOR_PIXELS;

// White circle with the brush's soft falloff and opacity baked into its
// alpha channel. One image serves as both the dab stamp (drawn into the
// stroke accumulator with SourceOver) and the per-pixel mask for the
// preview/composite paths (drawn with DestinationIn, which keeps the
// destination's color but multiplies its alpha by this image's alpha).
//
// Gradient stops: solid from the center out to `hardness` (fraction of the
// radius), then a smooth fade to fully transparent at the rim -- so
// hardness 1.0 is a crisp-edged circle and hardness 0.0 fades from the
// center outward. The 0.999 clamp keeps the middle stop strictly below the
// final stop at 1.0: two stops at the same position would make the
// solid-to-transparent order undefined.
// Mirrors _build_soft_circle in clonestamp_core.py; keep the two in sync.
QImage buildSoftCircle(int size, qreal hardness, int opacityPct)
{
    QImage img(size, size, QImage::Format_ARGB32_Premultiplied);
    img.fill(Qt::transparent);

    QPainter painter(&img);
    painter.setRenderHint(QPainter::Antialiasing, true);
    painter.setPen(Qt::NoPen);

    const qreal radius = size / 2.0;
    hardness = qBound(qreal(0.0), hardness, qreal(1.0));
    const int alpha = qBound(0, qRound(255.0 * opacityPct / 100.0), 255);

    QRadialGradient grad(size / 2.0, size / 2.0, qMax(radius, 0.5));
    grad.setColorAt(0.0, QColor(255, 255, 255, alpha));
    grad.setColorAt(qMin(hardness, qreal(0.999)), QColor(255, 255, 255, alpha));
    grad.setColorAt(1.0, QColor(255, 255, 255, 0));
    painter.setBrush(grad);
    painter.drawEllipse(QRectF(0, 0, size, size));
    painter.end();
    return img;
}

} // namespace

KisToolCloneStamp::KisToolCloneStamp(KoCanvasBase *canvas)
    : KisTool(canvas, KisCursor::crossCursor())
{
    setObjectName("tool_clonestamp");
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
    if (m_isPainting) {
        // A stroke was left open (e.g. tool switched mid-drag); flush the
        // accumulated dabs so far rather than losing them silently.
        finalizeStroke();
    }
    if (m_transaction) {
        m_transaction->commit(image()->undoAdapter());
        m_transaction.reset();
    }
    m_isPainting = false;
    m_isResizing = false;
    m_accImage = QImage();
    m_accBounds = QRect();
    m_useAccumulator = false;
    KisTool::deactivate();
}

bool KisToolCloneStamp::isValidPaintLayer(KisNodeSP node) const
{
    if (!node || !node->inherits("KisPaintLayer")) {
        return false;
    }
    KisPaintDeviceSP device = node->paintDevice();
    if (!device) {
        return false;
    }
    const KoColorSpace *cs = device->colorSpace();
    if (!cs) {
        return false;
    }
    return cs->colorModelId().id() == "RGBA" && cs->colorDepthId().id() == "U8";
}

KisPaintDeviceSP KisToolCloneStamp::sourceDeviceForSampling() const
{
    if (!m_hasSource || !image()) {
        return nullptr;
    }
    if (m_sampleScope == SampleScope::AllLayers) {
        return image()->projection();
    }
    if (!m_sourceNode || !isValidPaintLayer(m_sourceNode)) {
        return nullptr;
    }
    return m_sourceNode->paintDevice();
}

void KisToolCloneStamp::sampleSource(const QPointF &docPoint)
{
    KisNodeSP node = currentNode();
    if (!isValidPaintLayer(node)) {
        return;
    }
    m_sourceNode = node;
    m_sourcePoint = docPoint;
    m_hasSource = true;
    m_hasStrokeOffset = false;
    m_hasLastDabPoint = false;
    takeSourceSnapshot();
}

void KisToolCloneStamp::takeSourceSnapshot()
{
    // Freeze a copy of the source pixels at the moment they're sampled --
    // see m_sourceSnapshot in the header for why. Taken once here with the
    // sample scope current at Ctrl+click time; changing the scope combo
    // afterwards deliberately does NOT retake it (same semantics as
    // sample_source_point/_snapshot_source in clonestamp_core.py).
    m_sourceSnapshot = QImage();
    KisPaintDeviceSP srcDevice = sourceDeviceForSampling();
    if (!srcDevice || !image()) {
        return;
    }
    // The whole canvas rect, not the device's exact content bounds: every
    // read/write in this tool is already clipped to canvas bounds, so this
    // covers everything a stroke can touch.
    const QRect bounds = image()->bounds();
    const qint64 pixels = qint64(bounds.width()) * bounds.height();
    if (bounds.isEmpty() || pixels > MAX_SOURCE_SNAPSHOT_PIXELS) {
        // Too large to reasonably hold a whole extra copy of in memory;
        // leave the snapshot null so reads fall back to the live device.
        // That re-opens the smear-when-overlapping issue, but only on
        // documents this big, and at least keeps the tool working.
        return;
    }

    QByteArray bytes(bounds.width() * bounds.height() * static_cast<int>(srcDevice->pixelSize()), 0);
    srcDevice->readBytes(reinterpret_cast<quint8 *>(bytes.data()),
                         bounds.x(), bounds.y(), bounds.width(), bounds.height());
    // Krita's 8-bit RGBA colorspace stores straight BGRA bytes in memory,
    // matching QImage::Format_ARGB32 byte-for-byte on little-endian. The
    // copy() detaches from `bytes`, which dies at end of scope.
    m_sourceSnapshot = QImage(reinterpret_cast<const uchar *>(bytes.constData()),
                              bounds.width(), bounds.height(), QImage::Format_ARGB32).copy();
    m_snapshotLeft = bounds.x();
    m_snapshotTop = bounds.y();
}

QImage KisToolCloneStamp::readSourceImage(const QRect &rect) const
{
    if (!m_sourceSnapshot.isNull()) {
        // QImage::copy fills areas outside the snapshot with transparent
        // black, which is exactly the out-of-bounds behavior we want.
        const QImage slice = m_sourceSnapshot.copy(rect.translated(-m_snapshotLeft, -m_snapshotTop));
        return slice.convertToFormat(QImage::Format_ARGB32_Premultiplied);
    }

    KisPaintDeviceSP srcDevice = sourceDeviceForSampling();
    if (!srcDevice || rect.isEmpty()) {
        return QImage();
    }
    QByteArray bytes(rect.width() * rect.height() * static_cast<int>(srcDevice->pixelSize()), 0);
    srcDevice->readBytes(reinterpret_cast<quint8 *>(bytes.data()),
                         rect.x(), rect.y(), rect.width(), rect.height());
    const QImage wrapped(reinterpret_cast<const uchar *>(bytes.constData()),
                         rect.width(), rect.height(), QImage::Format_ARGB32);
    // convertToFormat always deep-copies here (formats differ), detaching
    // from `bytes` before it goes out of scope.
    return wrapped.convertToFormat(QImage::Format_ARGB32_Premultiplied);
}

const QImage &KisToolCloneStamp::softCircle() const
{
    const int size = qMax(1, m_brushSize);
    if (m_cachedCircle.isNull()
        || m_cachedCircleSize != size
        || m_cachedCircleHardness != m_brushHardness
        || m_cachedCircleOpacity != m_brushOpacity) {
        m_cachedCircle = buildSoftCircle(size, m_brushHardness, m_brushOpacity);
        m_cachedCircleSize = size;
        m_cachedCircleHardness = m_brushHardness;
        m_cachedCircleOpacity = m_brushOpacity;
    }
    return m_cachedCircle;
}

void KisToolCloneStamp::beginStroke(const QPointF &docPoint)
{
    if (!m_hasSource) {
        return;
    }
    KisNodeSP node = currentNode();
    if (!isValidPaintLayer(node)) {
        return;
    }

    if (!(m_aligned && m_hasStrokeOffset)) {
        m_strokeOffset = QPointF(m_sourcePoint.x() - docPoint.x(), m_sourcePoint.y() - docPoint.y());
        m_hasStrokeOffset = true;
    }
    m_hasLastDabPoint = false;
    m_isPainting = true;
    m_transaction.reset(new KisTransaction(node->paintDevice()));

    // Allocate the stroke accumulator (see m_accImage in the header). Where
    // the Python plugin refuses the stroke outright on an oversized canvas
    // (it can surface a user-facing error), a KisTool has no comparable
    // channel -- so here we fall back to the old per-dab immediate
    // compositing instead: degraded (opacity can build up past the ceiling
    // where dabs overlap) but working, which beats a silently dead tool.
    m_accBounds = QRect();
    const QRect canvasBounds = image()->bounds();
    const qint64 pixels = qint64(canvasBounds.width()) * canvasBounds.height();
    m_useAccumulator = pixels > 0 && pixels <= MAX_ACCUMULATOR_PIXELS;
    if (m_useAccumulator) {
        m_accImage = QImage(canvasBounds.width(), canvasBounds.height(),
                            QImage::Format_ARGB32_Premultiplied);
        if (m_accImage.isNull()) {
            // Allocation failure (out of memory) -- same fallback.
            m_useAccumulator = false;
        } else {
            m_accImage.fill(Qt::transparent);
            m_accLeft = canvasBounds.x();
            m_accTop = canvasBounds.y();
        }
    }
}

void KisToolCloneStamp::stampDabAt(const QPointF &dstCenter)
{
    if (!m_isPainting || !m_hasSource) {
        return;
    }
    if (m_useAccumulator) {
        recordDabToAccumulator(dstCenter);
    } else {
        stampDabImmediate(dstCenter);
    }
}

void KisToolCloneStamp::recordDabToAccumulator(const QPointF &dstCenter)
{
    // No paint-device access at all here -- a dab during a stroke is just a
    // soft circle drawn into the in-memory accumulator; the actual clone
    // (read source, mask, composite, write) happens once per stroke in
    // finalizeStroke. Mirrors _paint_dab_to_accumulator in
    // clonestamp_core.py.
    const int size = qMax(1, m_brushSize);
    const qreal half = size / 2.0;
    const QRect dabRect(qRound(dstCenter.x() - half), qRound(dstCenter.y() - half), size, size);

    m_accBounds = m_accBounds.isNull() ? dabRect : m_accBounds.united(dabRect);

    const int localX = dabRect.x() - m_accLeft;
    const int localY = dabRect.y() - m_accTop;
    const QRect clip = QRect(localX, localY, size, size).intersected(m_accImage.rect());
    if (clip.isEmpty()) {
        return;
    }

    // The circle is always built at full brush size and only the clipped
    // sub-rect of it is drawn, so the soft falloff stays centered on the
    // true brush circle even when the dab is cut off by a canvas edge.
    QPainter painter(&m_accImage);
    painter.setCompositionMode(QPainter::CompositionMode_SourceOver);
    painter.drawImage(clip.topLeft(), softCircle(),
                      QRect(clip.x() - localX, clip.y() - localY, clip.width(), clip.height()));
    painter.end();
}

void KisToolCloneStamp::stampDabImmediate(const QPointF &dstCenter)
{
    // Fallback path for canvases too large for the accumulator (see
    // beginStroke): read/mask/composite/write each dab directly. Overlapping
    // dabs at partial opacity can build past the opacity ceiling here --
    // known limitation of this path only.
    KisNodeSP dstNode = currentNode();
    if (!isValidPaintLayer(dstNode)) {
        return;
    }
    KisPaintDeviceSP dstDevice = dstNode->paintDevice();

    const QPointF srcCenter(dstCenter.x() + m_strokeOffset.x(), dstCenter.y() + m_strokeOffset.y());

    const int size = qMax(1, m_brushSize);
    const qreal half = size / 2.0;

    QRect srcRect(qRound(srcCenter.x() - half), qRound(srcCenter.y() - half), size, size);
    QRect dstRect(qRound(dstCenter.x() - half), qRound(dstCenter.y() - half), size, size);

    const QRect canvasBounds = image()->bounds();
    const QRect srcClip = srcRect.intersected(canvasBounds);
    const QRect dstClip = dstRect.intersected(canvasBounds);

    // Shrink both rects by whichever side needs it more, so they stay the
    // same size and pixel-aligned even when one side runs off the canvas.
    const int left = qMax(srcClip.left() - srcRect.left(), dstClip.left() - dstRect.left());
    const int top = qMax(srcClip.top() - srcRect.top(), dstClip.top() - dstRect.top());
    const int right = qMax(srcRect.right() - srcClip.right(), dstRect.right() - dstClip.right());
    const int bottom = qMax(srcRect.bottom() - srcClip.bottom(), dstRect.bottom() - dstClip.bottom());

    const int w = size - left - right;
    const int h = size - top - bottom;
    if (w <= 0 || h <= 0) {
        return; // entirely off one of the two areas; skip this dab
    }

    const int dstX = dstRect.x() + left;
    const int dstY = dstRect.y() + top;

    QImage srcImage = readSourceImage(QRect(srcRect.x() + left, srcRect.y() + top, w, h));
    if (srcImage.isNull()) {
        return;
    }

    QByteArray dstBytes(w * h * static_cast<int>(dstDevice->pixelSize()), 0);
    dstDevice->readBytes(reinterpret_cast<quint8 *>(dstBytes.data()), dstX, dstY, w, h);
    // Krita's 8-bit RGBA colorspace stores straight BGRA bytes in memory,
    // matching QImage::Format_ARGB32 byte-for-byte on little-endian.
    QImage dstImage(reinterpret_cast<const uchar *>(dstBytes.constData()), w, h, QImage::Format_ARGB32);
    dstImage = dstImage.convertToFormat(QImage::Format_ARGB32_Premultiplied);

    // Mask with the matching sub-rect of the FULL-size circle -- never a
    // circle rebuilt at the clipped w x h, which would re-center the falloff
    // on the clipped rectangle and make it visibly asymmetric near edges.
    // DestinationIn keeps srcImage's colors but multiplies its alpha by the
    // circle's alpha (falloff x opacity), i.e. per-pixel feathering.
    QPainter maskPainter(&srcImage);
    maskPainter.setCompositionMode(QPainter::CompositionMode_DestinationIn);
    maskPainter.drawImage(QPoint(0, 0), softCircle(), QRect(left, top, w, h));
    maskPainter.end();

    QPainter painter(&dstImage);
    painter.setCompositionMode(QPainter::CompositionMode_SourceOver);
    painter.drawImage(0, 0, srcImage);
    painter.end();

    const QImage resultImage = dstImage.convertToFormat(QImage::Format_ARGB32);
    dstDevice->writeBytes(resultImage.constBits(), dstX, dstY, w, h);
    dstDevice->setDirty(QRect(dstX, dstY, w, h));
}

void KisToolCloneStamp::finalizeStroke()
{
    // Composite the whole stroke in ONE pass: multiply the source by the
    // accumulated stroke alpha, then SourceOver onto the destination. This
    // is what keeps partial opacity honest -- however many dabs overlapped,
    // the accumulator's alpha never exceeds the chosen opacity, so neither
    // does the paint. Port of finalize_stroke in clonestamp_core.py.

    // Detach the accumulator state up front so every exit path below leaves
    // the tool clean (QImage is implicitly shared; this copy is cheap).
    const QImage accImage = m_accImage;
    const QRect accBounds = m_accBounds;
    const bool useAcc = m_useAccumulator;
    m_accImage = QImage();
    m_accBounds = QRect();
    m_useAccumulator = false;

    if (!useAcc || accImage.isNull() || accBounds.isNull()) {
        return; // immediate path already wrote everything, or no dabs landed
    }
    if (!image()) {
        return;
    }
    KisNodeSP dstNode = currentNode();
    if (!isValidPaintLayer(dstNode)) {
        return;
    }
    KisPaintDeviceSP dstDevice = dstNode->paintDevice();

    const QRect docBounds = image()->bounds();
    const QRect maskRect = accBounds.intersected(docBounds);
    if (maskRect.isEmpty()) {
        return;
    }

    // Source position = destination + stroke offset.
    const QRect srcFull = maskRect.translated(qRound(m_strokeOffset.x()), qRound(m_strokeOffset.y()));
    const QRect dstFull = maskRect;

    // Both sides clip against the canvas (reads outside a device's content
    // return transparent anyway, and dabs were only ever recorded inside
    // canvas bounds).
    const QRect srcClip = srcFull.intersected(docBounds);
    const QRect dstClip = dstFull.intersected(docBounds);
    if (srcClip.isEmpty() || dstClip.isEmpty()) {
        return;
    }

    // Shrink both rects by whichever side needs it more, so they stay the
    // same size and pixel-aligned even when one side runs off the canvas.
    const int left = qMax(srcClip.left() - srcFull.left(), dstClip.left() - dstFull.left());
    const int top = qMax(srcClip.top() - srcFull.top(), dstClip.top() - dstFull.top());
    const int right = qMax(srcFull.right() - srcClip.right(), dstFull.right() - dstClip.right());
    const int bottom = qMax(srcFull.bottom() - srcClip.bottom(), dstFull.bottom() - dstClip.bottom());

    const int w = maskRect.width() - left - right;
    const int h = maskRect.height() - top - bottom;
    if (w <= 0 || h <= 0) {
        return;
    }

    const QRect srcRect(srcFull.x() + left, srcFull.y() + top, w, h);
    const QRect dstRect(dstFull.x() + left, dstFull.y() + top, w, h);

    // Read source once -- from the frozen snapshot when available (see
    // takeSourceSnapshot), else live.
    QImage srcImage = readSourceImage(srcRect);
    if (srcImage.isNull()) {
        return;
    }

    // Read destination once.
    QByteArray dstBytes(w * h * static_cast<int>(dstDevice->pixelSize()), 0);
    dstDevice->readBytes(reinterpret_cast<quint8 *>(dstBytes.data()), dstRect.x(), dstRect.y(), w, h);
    QImage dstImage(reinterpret_cast<const uchar *>(dstBytes.constData()), w, h, QImage::Format_ARGB32);
    dstImage = dstImage.convertToFormat(QImage::Format_ARGB32_Premultiplied);

    // Slice the accumulator: m_accLeft/m_accTop is the accumulator origin
    // (= canvas origin), not accBounds.
    const QImage maskSlice = accImage.copy(dstRect.x() - m_accLeft, dstRect.y() - m_accTop, w, h);

    // Step 1: multiply source by the stroke mask's alpha (DestinationIn
    // keeps srcImage's colors, scales its alpha per-pixel by the mask's).
    QPainter maskPainter(&srcImage);
    maskPainter.setCompositionMode(QPainter::CompositionMode_DestinationIn);
    maskPainter.drawImage(0, 0, maskSlice);
    maskPainter.end();

    // Step 2: composite masked source over destination (SourceOver).
    QPainter painter(&dstImage);
    painter.setCompositionMode(QPainter::CompositionMode_SourceOver);
    painter.drawImage(0, 0, srcImage);
    painter.end();

    const QImage resultImage = dstImage.convertToFormat(QImage::Format_ARGB32);
    dstDevice->writeBytes(resultImage.constBits(), dstRect.x(), dstRect.y(), w, h);
    dstDevice->setDirty(dstRect);
}

void KisToolCloneStamp::updateOutline(const QPointF &pixelPoint)
{
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

    if (!image()) {
        return;
    }

    const qreal half = qMax(1, m_brushSize) / 2.0;
    QRectF pixelRect(pixelPoint.x() - half, pixelPoint.y() - half, half * 2, half * 2);
    if (m_hasPreviewSource) {
        QRectF srcRect(m_previewSourcePoint.x() - half, m_previewSourcePoint.y() - half, half * 2, half * 2);
        pixelRect |= srcRect;
    }
    pixelRect = pixelRect.adjusted(-4, -4, 4, 4);
    const QRectF docRect = image()->pixelToDocument(pixelRect);

    if (!m_lastOutlineUpdateRect.isEmpty()) {
        canvas()->updateCanvas(m_lastOutlineUpdateRect);
    }
    canvas()->updateCanvas(docRect);
    m_lastOutlineUpdateRect = docRect;
}

QImage KisToolCloneStamp::buildPreviewPatch(const QPointF &srcCenterPixels) const
{
    if (!image()) {
        return QImage();
    }

    const int size = qMax(1, m_brushSize);
    const qreal half = size / 2.0;

    const QRect srcRect(qRound(srcCenterPixels.x() - half), qRound(srcCenterPixels.y() - half), size, size);
    const QRect clip = srcRect.intersected(image()->bounds());
    if (clip.isEmpty()) {
        return QImage();
    }

    QImage clipImage = readSourceImage(clip);
    if (clipImage.isNull()) {
        return QImage();
    }

    // Compose at full brush size with the in-bounds content drawn at its
    // offset, then mask with the full-size circle -- so the soft falloff
    // stays centered on the true brush circle even when the source area is
    // partly off-canvas (masking the clipped rect directly would re-center
    // the falloff on the clipped rectangle).
    QImage patch(size, size, QImage::Format_ARGB32_Premultiplied);
    patch.fill(Qt::transparent);
    QPainter painter(&patch);
    painter.drawImage(clip.x() - srcRect.x(), clip.y() - srcRect.y(), clipImage);
    painter.setCompositionMode(QPainter::CompositionMode_DestinationIn);
    painter.drawImage(0, 0, softCircle());
    painter.end();
    return patch;
}

QImage KisToolCloneStamp::cachedPreviewPatch(const QPointF &srcCenterPixels) const
{
    // paint() runs at Krita's repaint cadence; rebuilding the preview patch
    // (device read + mask) every time is the expensive part, so refresh it
    // at most every 200ms and reuse the last frame in between. The content
    // can lag the cursor by up to that interval -- the same trade-off the
    // Python docker's _refreshPreviewCache makes at the same ~5Hz budget.
    // Brush parameter changes refresh immediately so the preview never
    // shows a stale size/hardness/opacity.
    const bool stale = m_previewCache.isNull()
        || !m_previewCacheTimer.isValid()
        || m_previewCacheTimer.elapsed() >= 200
        || m_previewCacheSize != m_brushSize
        || m_previewCacheHardness != m_brushHardness
        || m_previewCacheOpacity != m_brushOpacity;
    if (stale) {
        m_previewCache = buildPreviewPatch(srcCenterPixels);
        m_previewCacheTimer.start();
        m_previewCacheSize = m_brushSize;
        m_previewCacheHardness = m_brushHardness;
        m_previewCacheOpacity = m_brushOpacity;
    }
    return m_previewCache;
}

void KisToolCloneStamp::paint(QPainter &gc, const KoViewConverter &converter)
{
    if (!m_hasHoverPoint || !image()) {
        return;
    }
    const qreal half = qMax(1, m_brushSize) / 2.0;
    const QRectF pixelRect(m_hoverPoint.x() - half, m_hoverPoint.y() - half, half * 2, half * 2);
    const QRectF docRect = image()->pixelToDocument(pixelRect);
    const QRectF viewRect = converter.documentToView(docRect);

    if (m_hasPreviewSource) {
        const QImage preview = cachedPreviewPatch(m_previewSourcePoint);
        if (!preview.isNull()) {
            gc.save();
            gc.setOpacity(0.6);
            gc.drawImage(viewRect, preview);
            gc.restore();
        }
    }

    {
        const QPointF dstCenter = viewRect.center();
        const qreal dstCrossRadius = 6.0;

        gc.save();
        gc.setPen(QPen(Qt::white, 1));
        gc.setBrush(Qt::NoBrush);
        gc.drawEllipse(viewRect);
        gc.drawLine(QPointF(dstCenter.x() - dstCrossRadius, dstCenter.y()), QPointF(dstCenter.x() + dstCrossRadius, dstCenter.y()));
        gc.drawLine(QPointF(dstCenter.x(), dstCenter.y() - dstCrossRadius), QPointF(dstCenter.x(), dstCenter.y() + dstCrossRadius));
        gc.restore();
    }

    if (m_hasPreviewSource) {
        const QRectF srcPixelRect(m_previewSourcePoint.x() - half, m_previewSourcePoint.y() - half, half * 2, half * 2);
        const QRectF srcDocRect = image()->pixelToDocument(srcPixelRect);
        const QRectF srcViewRect = converter.documentToView(srcDocRect);
        const QPointF center = srcViewRect.center();
        const qreal crossRadius = 6.0;

        gc.save();
        gc.setPen(QPen(QColor(255, 255, 255, 120), 1));
        gc.setBrush(Qt::NoBrush);
        gc.drawEllipse(srcViewRect);
        gc.drawLine(QPointF(center.x() - crossRadius, center.y()), QPointF(center.x() + crossRadius, center.y()));
        gc.drawLine(QPointF(center.x(), center.y() - crossRadius), QPointF(center.x(), center.y() + crossRadius));
        gc.restore();
    }
}

void KisToolCloneStamp::beginPrimaryAction(KoPointerEvent *event)
{
    const QPointF pixelPoint = convertToPixelCoord(event);

    beginStroke(pixelPoint);
    if (m_isPainting) {
        stampDabAt(pixelPoint);
        m_lastDabPoint = pixelPoint;
        m_hasLastDabPoint = true;
    }
    updateOutline(pixelPoint);
}

void KisToolCloneStamp::continuePrimaryAction(KoPointerEvent *event)
{
    if (!m_isPainting) {
        return;
    }

    const QPointF pixelPoint = convertToPixelCoord(event);
    // Dab spacing scales with brush size: overlapping soft circles union to
    // a smooth mask, so at 15% of the diameter (well under Photoshop's 25%
    // default brush spacing) no scalloping is visible, while a 250px brush
    // stamps ~19x fewer dabs than the old fixed 2px spacing did.
    const qreal minSpacing = qMax(qreal(2.0), m_brushSize * qreal(0.15));
    if (m_hasLastDabPoint) {
        const qreal dx = pixelPoint.x() - m_lastDabPoint.x();
        const qreal dy = pixelPoint.y() - m_lastDabPoint.y();
        if ((dx * dx + dy * dy) < (minSpacing * minSpacing)) {
            return;
        }
    }

    stampDabAt(pixelPoint);
    m_lastDabPoint = pixelPoint;
    m_hasLastDabPoint = true;
    updateOutline(pixelPoint);
}

void KisToolCloneStamp::endPrimaryAction(KoPointerEvent *event)
{
    Q_UNUSED(event);

    if (m_isPainting) {
        m_isPainting = false;
        m_hasLastDabPoint = false;
        finalizeStroke();
        if (m_transaction) {
            m_transaction->commit(image()->undoAdapter());
            m_transaction.reset();
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
        // hardness, dragging down softens -- matches Photoshop's on-canvas
        // brush resize convention.
        const int dy = event->pos().y() - m_resizeStartWidgetPos.y();

        m_brushSize = qBound(1, m_resizeStartSize + dx, 2000);
        const int hardnessPercent = qBound(0, m_resizeStartHardnessPercent - dy, 100);
        m_brushHardness = hardnessPercent / 100.0;

        if (m_sizeSpin) {
            QSignalBlocker blocker(m_sizeSpin);
            m_sizeSpin->setValue(m_brushSize);
        }
        if (m_hardnessSpin) {
            QSignalBlocker blocker(m_hardnessSpin);
            m_hardnessSpin->setValue(hardnessPercent);
        }

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

QWidget *KisToolCloneStamp::createOptionWidget()
{
    if (m_optionWidget) {
        return m_optionWidget;
    }

    QWidget *widget = new QWidget();
    QVBoxLayout *layout = new QVBoxLayout(widget);

    QHBoxLayout *sizeRow = new QHBoxLayout();
    sizeRow->addWidget(new QLabel(i18n("Size:")));
    QSpinBox *sizeSpin = new QSpinBox();
    sizeSpin->setRange(1, 2000);
    sizeSpin->setSuffix(i18n(" px"));
    sizeSpin->setValue(m_brushSize);
    connect(sizeSpin, QOverload<int>::of(&QSpinBox::valueChanged), this, [this](int value) {
        m_brushSize = value;
    });
    sizeRow->addWidget(sizeSpin);
    layout->addLayout(sizeRow);
    m_sizeSpin = sizeSpin;

    QHBoxLayout *hardnessRow = new QHBoxLayout();
    hardnessRow->addWidget(new QLabel(i18n("Hardness:")));
    QSpinBox *hardnessSpin = new QSpinBox();
    hardnessSpin->setRange(0, 100);
    hardnessSpin->setSuffix(i18n(" %"));
    hardnessSpin->setValue(qRound(m_brushHardness * 100));
    connect(hardnessSpin, QOverload<int>::of(&QSpinBox::valueChanged), this, [this](int value) {
        m_brushHardness = value / 100.0;
    });
    hardnessRow->addWidget(hardnessSpin);
    layout->addLayout(hardnessRow);
    m_hardnessSpin = hardnessSpin;

    QHBoxLayout *opacityRow = new QHBoxLayout();
    opacityRow->addWidget(new QLabel(i18n("Opacity:")));
    QSpinBox *opacitySpin = new QSpinBox();
    opacitySpin->setRange(0, 100);
    opacitySpin->setSuffix(i18n(" %"));
    opacitySpin->setValue(m_brushOpacity);
    connect(opacitySpin, QOverload<int>::of(&QSpinBox::valueChanged), this, [this](int value) {
        m_brushOpacity = value;
    });
    opacityRow->addWidget(opacitySpin);
    layout->addLayout(opacityRow);

    QCheckBox *alignedCheck = new QCheckBox(i18n("Aligned"));
    alignedCheck->setChecked(m_aligned);
    connect(alignedCheck, &QCheckBox::toggled, this, [this](bool checked) {
        m_aligned = checked;
        // Non-Aligned means every new stroke resamples from the original
        // source point, so drop any offset fixed by a previous stroke.
        if (!checked) {
            m_hasStrokeOffset = false;
        }
    });
    layout->addWidget(alignedCheck);

    QHBoxLayout *sampleRow = new QHBoxLayout();
    sampleRow->addWidget(new QLabel(i18n("Sample:")));
    QComboBox *sampleCombo = new QComboBox();
    sampleCombo->addItem(i18n("Current Layer"));
    sampleCombo->addItem(i18n("All Layers"));
    sampleCombo->setCurrentIndex(m_sampleScope == SampleScope::AllLayers ? 1 : 0);
    connect(sampleCombo, QOverload<int>::of(&QComboBox::currentIndexChanged), this, [this](int index) {
        m_sampleScope = (index == 1) ? SampleScope::AllLayers : SampleScope::CurrentLayer;
        // Deliberately does not retake the source snapshot -- the scope is
        // captured at Ctrl+click time, same as the Python plugin.
    });
    sampleRow->addWidget(sampleCombo);
    layout->addLayout(sampleRow);

    layout->addStretch();
    m_optionWidget = widget;
    return m_optionWidget;
}
