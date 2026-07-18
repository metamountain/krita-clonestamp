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

QImage buildRadialMask(int w, int h, qreal hardness)
{
    QImage mask(w, h, QImage::Format_ARGB32);
    mask.fill(Qt::transparent);

    QPainter painter(&mask);
    painter.setRenderHint(QPainter::Antialiasing, true);
    painter.setPen(Qt::NoPen);

    const qreal radius = qMin(w, h) / 2.0;
    hardness = qBound(qreal(0.0), hardness, qreal(1.0));

    QRadialGradient grad(w / 2.0, h / 2.0, qMax(radius, 0.5));
    grad.setColorAt(0.0, QColor(255, 255, 255, 255));
    grad.setColorAt(qMin(hardness, qreal(0.999)), QColor(255, 255, 255, 255));
    grad.setColorAt(1.0, QColor(255, 255, 255, 0));
    painter.setBrush(grad);
    painter.drawEllipse(QRectF(0, 0, w, h));
    painter.end();
    return mask;
}

void applyRadialMask(QImage &image, qreal hardness)
{
    QImage mask = buildRadialMask(image.width(), image.height(), hardness);
    QPainter painter(&image);
    painter.setCompositionMode(QPainter::CompositionMode_DestinationIn);
    painter.drawImage(0, 0, mask);
    painter.end();
}

void scaleAlpha(QImage &image, qreal factor)
{
    if (factor >= 1.0) {
        return;
    }
    QImage overlay(image.size(), QImage::Format_ARGB32);
    overlay.fill(QColor(0, 0, 0, qBound(0, int(255 * factor), 255)));
    QPainter painter(&image);
    painter.setCompositionMode(QPainter::CompositionMode_DestinationIn);
    painter.drawImage(0, 0, overlay);
    painter.end();
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
    if (m_transaction) {
        // A stroke was left open (e.g. tool switched mid-drag); commit
        // whatever was painted so far rather than losing it silently.
        m_transaction->commit(image()->undoAdapter());
        m_transaction.reset();
    }
    m_isPainting = false;
    m_isResizing = false;
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
}

void KisToolCloneStamp::stampDabAt(const QPointF &dstCenter)
{
    if (!m_isPainting || !m_hasSource) {
        return;
    }

    KisNodeSP dstNode = currentNode();
    if (!isValidPaintLayer(dstNode)) {
        return;
    }

    KisPaintDeviceSP srcDevice = sourceDeviceForSampling();
    KisPaintDeviceSP dstDevice = dstNode->paintDevice();
    if (!srcDevice) {
        return;
    }

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

    const int srcX = srcRect.x() + left;
    const int srcY = srcRect.y() + top;
    const int dstX = dstRect.x() + left;
    const int dstY = dstRect.y() + top;

    QByteArray srcBytes(w * h * static_cast<int>(dstDevice->pixelSize()), 0);
    QByteArray dstBytes(w * h * static_cast<int>(dstDevice->pixelSize()), 0);

    srcDevice->readBytes(reinterpret_cast<quint8 *>(srcBytes.data()), srcX, srcY, w, h);
    dstDevice->readBytes(reinterpret_cast<quint8 *>(dstBytes.data()), dstX, dstY, w, h);

    // Krita's 8-bit RGBA colorspace stores straight BGRA bytes in memory,
    // matching QImage::Format_ARGB32 byte-for-byte on little-endian.
    QImage srcImage(reinterpret_cast<const uchar *>(srcBytes.constData()), w, h, QImage::Format_ARGB32);
    srcImage = srcImage.convertToFormat(QImage::Format_ARGB32_Premultiplied);
    QImage dstImage(reinterpret_cast<const uchar *>(dstBytes.constData()), w, h, QImage::Format_ARGB32);
    dstImage = dstImage.convertToFormat(QImage::Format_ARGB32_Premultiplied);

    applyRadialMask(srcImage, m_brushHardness);
    scaleAlpha(srcImage, m_brushOpacity / 100.0);

    QPainter painter(&dstImage);
    painter.setCompositionMode(QPainter::CompositionMode_SourceOver);
    painter.drawImage(0, 0, srcImage);
    painter.end();

    const QImage resultImage = dstImage.convertToFormat(QImage::Format_ARGB32);
    dstDevice->writeBytes(resultImage.constBits(), dstX, dstY, w, h);
    dstDevice->setDirty(QRect(dstX, dstY, w, h));
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
    KisPaintDeviceSP srcDevice = sourceDeviceForSampling();
    if (!srcDevice) {
        return QImage();
    }

    const int size = qMax(1, m_brushSize);
    const qreal half = size / 2.0;

    QRect srcRect(qRound(srcCenterPixels.x() - half), qRound(srcCenterPixels.y() - half), size, size);
    const QRect clip = srcRect.intersected(image()->bounds());
    if (clip.isEmpty()) {
        return QImage();
    }

    QByteArray bytes(clip.width() * clip.height() * static_cast<int>(srcDevice->pixelSize()), 0);
    srcDevice->readBytes(reinterpret_cast<quint8 *>(bytes.data()), clip.x(), clip.y(), clip.width(), clip.height());

    QImage srcImage(reinterpret_cast<const uchar *>(bytes.constData()), clip.width(), clip.height(), QImage::Format_ARGB32);
    srcImage = srcImage.copy().convertToFormat(QImage::Format_ARGB32_Premultiplied);
    applyRadialMask(srcImage, m_brushHardness);
    scaleAlpha(srcImage, m_brushOpacity / 100.0);
    return srcImage;
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
        const QImage preview = buildPreviewPatch(m_previewSourcePoint);
        if (!preview.isNull()) {
            gc.save();
            gc.setOpacity(0.6);
            gc.drawImage(viewRect, preview);
            gc.restore();
        }
    }

    const qreal crossRadius = 6.0;

    // Destination ring + centre crosshair (drawn solid/opaque).
    gc.save();
    gc.setPen(QPen(Qt::white, 1));
    gc.setBrush(Qt::NoBrush);
    gc.drawEllipse(viewRect);
    const QPointF dstCenter = viewRect.center();
    gc.drawLine(QPointF(dstCenter.x() - crossRadius, dstCenter.y()), QPointF(dstCenter.x() + crossRadius, dstCenter.y()));
    gc.drawLine(QPointF(dstCenter.x(), dstCenter.y() - crossRadius), QPointF(dstCenter.x(), dstCenter.y() + crossRadius));
    gc.restore();

    if (m_hasPreviewSource) {
        const QRectF srcPixelRect(m_previewSourcePoint.x() - half, m_previewSourcePoint.y() - half, half * 2, half * 2);
        const QRectF srcDocRect = image()->pixelToDocument(srcPixelRect);
        const QRectF srcViewRect = converter.documentToView(srcDocRect);
        const QPointF center = srcViewRect.center();

        // Source ring + centre crosshair, drawn paler than the destination so
        // the source reads as the fainter of the synced pair.
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
    const qreal minSpacing = 2.0;
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
    });
    sampleRow->addWidget(sampleCombo);
    layout->addLayout(sampleRow);

    layout->addStretch();
    m_optionWidget = widget;
    return m_optionWidget;
}
