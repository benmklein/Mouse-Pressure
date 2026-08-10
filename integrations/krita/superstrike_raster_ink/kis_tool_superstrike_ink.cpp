/*
 * SPDX-FileCopyrightText: 2026 Ben Klein and contributors
 * SPDX-License-Identifier: GPL-2.0-or-later
 */

#include "kis_tool_superstrike_ink.h"
#include "perfect_freehand_outline.h"

#include <algorithm>
#include <cmath>
#include <memory>
#include <QCheckBox>
#include <QComboBox>
#include <QLabel>
#include <QPainterPath>
#include <QTabletEvent>
#include <QtMath>

#include <kconfiggroup.h>
#include <ksharedconfig.h>
#include <klocalizedstring.h>

#include <KoCanvasBase.h>
#include <canvas/kis_canvas2.h>
#include <canvas/kis_coordinates_converter.h>
#include <kis_cursor.h>
#include <kis_figure_painting_tool_helper.h>
#include <kis_paintop_preset.h>
#include <kis_paintop_settings.h>
#include <kis_slider_spin_box.h>
#include <kis_smoothing_options.h>
#include <kis_tool_freehand_helper.h>
#include <kundo2magicstring.h>

namespace {
constexpr qreal STARTUP_CORRECTION_MAX_MS = 65.0;
constexpr qreal MINIMUM_START_PRESSURE_RATIO = 0.45;

struct ReplaySample
{
    QPointF point;
    qreal distance {0.0};
    int leftIndex {0};
    int rightIndex {0};
    qreal amount {0.0};
    qreal pressure {0.0};
    ulong timeMs {0};
};

qreal interpolate(qreal left, qreal right, qreal amount)
{
    return left + (right - left) * amount;
}

std::unique_ptr<QTabletEvent> copyTabletEventWithPressure(
    const QTabletEvent *source,
    qreal pressure)
{
    if (!source) {
        return {};
    }
    auto event = std::make_unique<QTabletEvent>(
        source->type(),
        source->posF(),
        source->globalPosF(),
        source->deviceType(),
        source->pointerType(),
        qBound<qreal>(0.0, pressure, 1.0),
        source->xTilt(),
        source->yTilt(),
        source->tangentialPressure(),
        source->rotation(),
        source->z(),
        source->modifiers(),
        source->uniqueId(),
        source->button(),
        source->buttons());
    event->setTimestamp(source->timestamp());
    return event;
}

struct DetectedTail
{
    bool valid {false};
    qreal length {0.0};
    qreal bodyPressure {0.0};
};

DetectedTail detectTail(const QVector<ReplaySample> &samples,
                        qreal totalLength,
                        bool fromStart,
                        qreal maximumLength)
{
    const qreal limit = qMin(qMax<qreal>(8.0, maximumLength),
                             totalLength * 0.45);
    QVector<int> indices;
    for (int offset = 0; offset < samples.size(); ++offset) {
        const int index = fromStart ? offset : samples.size() - 1 - offset;
        const qreal edgeDistance = fromStart
            ? samples[index].distance
            : totalLength - samples[index].distance;
        if (edgeDistance > limit) {
            break;
        }
        indices.push_back(index);
    }
    if (indices.size() < 4) {
        return {};
    }

    QVector<qreal> orderedPressures;
    orderedPressures.reserve(indices.size());
    for (int index : indices) {
        orderedPressures.push_back(samples[index].pressure);
    }
    std::sort(orderedPressures.begin(), orderedPressures.end());
    const int percentileIndex = qRound((orderedPressures.size() - 1) * 0.8);
    const qreal bodyPressure = orderedPressures[percentileIndex];
    if (bodyPressure <= 0.000001 ||
        samples[indices.front()].pressure >= bodyPressure * 0.65) {
        return {};
    }

    QVector<qreal> smoothed;
    smoothed.reserve(indices.size());
    for (int position = 0; position < indices.size(); ++position) {
        const int left = qMax(0, position - 2);
        const int right = qMin(indices.size(), position + 3);
        qreal sum = 0.0;
        for (int neighbor = left; neighbor < right; ++neighbor) {
            sum += samples[indices[neighbor]].pressure;
        }
        smoothed.push_back(sum / (right - left));
    }

    for (int position = 0; position < indices.size(); ++position) {
        const ReplaySample &sample = samples[indices[position]];
        const qreal edgeDistance = fromStart
            ? sample.distance
            : totalLength - sample.distance;
        if (edgeDistance < 3.0 || smoothed[position] < bodyPressure * 0.8) {
            continue;
        }
        const int sustainEnd = qMin(indices.size(), position + 7);
        bool sustained = true;
        for (int neighbor = position; neighbor < sustainEnd; ++neighbor) {
            if (smoothed[neighbor] < bodyPressure * 0.6) {
                sustained = false;
                break;
            }
        }
        if (sustained) {
            if (fromStart) {
                const qint64 elapsedMs = qint64(sample.timeMs) -
                    qint64(samples.front().timeMs);
                if (elapsedMs < 0 || elapsedMs > STARTUP_CORRECTION_MAX_MS) {
                    return {};
                }
            }
            return {true,
                    edgeDistance,
                    bodyPressure};
        }
    }
    return {};
}

QVector<ReplaySample> prepareReplaySamples(
    const QVector<QPointF> &points,
    const std::vector<KoPointerEventWrapper> &events,
    bool adaptiveTails,
    qreal maximumTailLengthPx)
{
    QVector<ReplaySample> samples;
    if (points.isEmpty() || points.size() != int(events.size())) {
        return samples;
    }

    QVector<qreal> cumulative;
    cumulative.reserve(points.size());
    cumulative.push_back(0.0);
    for (int index = 1; index < points.size(); ++index) {
        const QPointF delta = points[index] - points[index - 1];
        cumulative.push_back(cumulative.back() +
                             std::hypot(delta.x(), delta.y()));
    }
    const qreal totalLength = cumulative.back();
    if (totalLength <= 0.000001) {
        samples.push_back({points.front(),
                           0.0,
                           0,
                           0,
                           0.0,
                           events.front().event.pressure(),
                           events.front().event.time()});
        return samples;
    }

    samples.reserve(qCeil(totalLength) + 1);
    const int sampleCount = qCeil(totalLength);
    for (int sampleIndex = 0; sampleIndex <= sampleCount; ++sampleIndex) {
        const qreal distance = qMin<qreal>(sampleIndex, totalLength);
        int leftIndex = 0;
        int rightIndex = 0;
        qreal amount = 0.0;
        if (distance >= totalLength) {
            leftIndex = points.size() - 1;
            rightIndex = leftIndex;
        } else if (distance > 0.0) {
            const auto upper = std::upper_bound(cumulative.cbegin(),
                                                cumulative.cend(),
                                                distance);
            rightIndex = int(std::distance(cumulative.cbegin(), upper));
            leftIndex = qMax(0, rightIndex - 1);
            const qreal span = cumulative[rightIndex] - cumulative[leftIndex];
            amount = span <= 0.000001
                ? 1.0
                : (distance - cumulative[leftIndex]) / span;
        }

        const qreal leftPressure = events[size_t(leftIndex)].event.pressure();
        const qreal rightPressure = events[size_t(rightIndex)].event.pressure();
        const qreal leftTime = events[size_t(leftIndex)].event.time();
        const qreal rightTime = events[size_t(rightIndex)].event.time();
        samples.push_back({
            points[leftIndex] +
                (points[rightIndex] - points[leftIndex]) * amount,
            distance,
            leftIndex,
            rightIndex,
            amount,
            interpolate(leftPressure, rightPressure, amount),
            ulong(qRound64(interpolate(leftTime, rightTime, amount)))
        });
    }

    if (!adaptiveTails || samples.size() < 4 || totalLength < 4.0) {
        return samples;
    }
    const DetectedTail startTail = detectTail(samples,
                                              totalLength,
                                              true,
                                              maximumTailLengthPx);
    const DetectedTail endTail = detectTail(samples,
                                            totalLength,
                                            false,
                                            maximumTailLengthPx);
    for (ReplaySample &sample : samples) {
        if (startTail.valid && sample.distance < startTail.length) {
            const qreal progress = qBound<qreal>(
                0.0, sample.distance / startTail.length, 1.0);
            const qreal correctedPressure = startTail.bodyPressure *
                (MINIMUM_START_PRESSURE_RATIO +
                 (1.0 - MINIMUM_START_PRESSURE_RATIO) *
                     std::pow(progress, 0.55));
            sample.pressure = qMax(sample.pressure, correctedPressure);
        }
        const qreal remaining = totalLength - sample.distance;
        if (endTail.valid && remaining < endTail.length) {
            const qreal progress = qBound<qreal>(
                0.0, remaining / endTail.length, 1.0);
            sample.pressure = endTail.bodyPressure *
                (0.06 + 0.94 * std::pow(progress, 0.55));
        }
        sample.pressure = qBound<qreal>(0.0, sample.pressure, 1.0);
    }
    return samples;
}
}

class SuperstrikeReplayHelper : public KisToolFreehandHelper
{
public:
    using KisToolFreehandHelper::KisToolFreehandHelper;

    void cancelPreview()
    {
        cancelPaint();
    }
};

KisToolSuperstrikeInk::KisToolSuperstrikeInk(KoCanvasBase *canvas)
    : KisToolFreehand(canvas,
                      KisCursor::load("tool_freehand_cursor.xpm", 2, 2),
                      kundo2_i18n("Superstrike Raster Ink Stroke"),
                      false)
{
    setObjectName("tool_superstrike_raster_ink");
    setIsOpacityPresetMode(true);

    m_replayHelper = new SuperstrikeReplayHelper(
        paintingInformationBuilder(),
        canvas->resourceManager(),
        kundo2_i18n("Superstrike Raster Ink Stroke"),
        new KisSmoothingOptions(false));
    connect(m_replayHelper,
            SIGNAL(requestExplicitUpdateOutline()),
            SLOT(explicitUpdateOutline()));
    resetHelper(m_replayHelper);
}

KisToolSuperstrikeInk::~KisToolSuperstrikeInk() = default;

void KisToolSuperstrikeInk::activate(const QSet<KoShape *> &shapes)
{
    KisToolFreehand::activate(shapes);
    const KConfigGroup config = KSharedConfig::openConfig()->group(toolId());
    const int storedMode = config.readEntry("inkMode", 0);
    m_inkMode = static_cast<InkMode>(qBound(0, storedMode, 2));
    m_pathAssistStrength = config.readEntry("pathAssistStrength", 0.2);
    m_pressureSmoothing = config.readEntry("pressureSmoothing", 0.25);
    m_finalAmount = config.readEntry("finalAmount", 0.0);
    m_finalPasses = config.readEntry("finalPasses", 0);
    m_adaptiveTails = config.readEntry("adaptiveTails", true);
    m_maximumTailLengthPx = config.readEntry("maximumTailLengthPx", 72.0);
    m_perfectFreehandStreamline = config.readEntry(
        "perfectFreehandStreamline", 0.5);
    m_perfectFreehandSmoothing = config.readEntry(
        "perfectFreehandSmoothing", 0.5);
    m_perfectFreehandThinning = config.readEntry(
        "perfectFreehandThinning", 1.0);
}

void KisToolSuperstrikeInk::rememberEvent(KoPointerEvent *event)
{
    m_events.push_back(event->deepCopyEvent());
    m_rawPoints.push_back(convertDocumentToWidget(event->point));
}

QPointF KisToolSuperstrikeInk::documentPositionFromWidget(
    const QPointF &point) const
{
    const auto *kritaCanvas = dynamic_cast<KisCanvas2 *>(canvas());
    if (!kritaCanvas) {
        return point;
    }
    return kritaCanvas->coordinatesConverter()->widgetToDocument(point);
}

QPointF KisToolSuperstrikeInk::imagePositionFromWidget(
    const QPointF &point) const
{
    const auto *kritaCanvas = dynamic_cast<KisCanvas2 *>(canvas());
    if (!kritaCanvas) {
        return point;
    }
    return kritaCanvas->coordinatesConverter()->widgetToImage(point);
}

QPointF KisToolSuperstrikeInk::pathAssistedPosition(KoPointerEvent *event,
                                                    bool begin)
{
    const QPointF rawPoint = convertDocumentToWidget(event->point);
    if (begin || !m_pathAssistInitialized) {
        m_pathAssistPoint = rawPoint;
        m_pathAssistInitialized = true;
    } else {
        // Perfect Freehand's streamline is a causal interpolation toward the
        // newest input point. Keep the range deliberately mild here: even at
        // maximum assistance this trails by less than one input sample, so
        // the active Krita brush remains responsive and owns all pressure,
        // texture, spacing, opacity, and sensor behavior.
        const qreal strength = qBound<qreal>(
            0.0, m_pathAssistStrength * zoomAssistMultiplier(), 1.0);
        const qreal follow = 1.0 - 0.35 * strength;
        m_pathAssistPoint += (rawPoint - m_pathAssistPoint) * follow;
    }
    return documentPositionFromWidget(m_pathAssistPoint);
}

qreal KisToolSuperstrikeInk::zoomAssistMultiplier() const
{
    const auto *kritaCanvas = dynamic_cast<KisCanvas2 *>(canvas());
    if (!kritaCanvas) {
        return 1.0;
    }
    const qreal zoom = qMax<qreal>(0.01,
        kritaCanvas->coordinatesConverter()->effectiveZoom());
    if (zoom >= 1.0) {
        return 1.0;
    }
    // At low canvas zoom, one integer screen-pixel input step expands into
    // several image pixels. Increase assistance gradually to hide that
    // quantization when the stroke is inspected at 100% later.
    return 1.0 + 0.5 * std::log2(1.0 / zoom);
}

qreal KisToolSuperstrikeInk::assistedPressure(qreal pressure,
                                              ulong timeMs,
                                              bool begin)
{
    pressure = qBound<qreal>(0.0, pressure, 1.0);
    const qreal smoothing = qBound<qreal>(
        0.0, m_pressureSmoothing * zoomAssistMultiplier(), 1.0);
    if (begin || smoothing <= 0.000001) {
        m_assistedPressure = pressure;
        m_assistedPressureTimeMs = timeMs;
        return pressure;
    }
    qreal dtSeconds = timeMs > m_assistedPressureTimeMs
        ? qreal(timeMs - m_assistedPressureTimeMs) / 1000.0
        : 1.0 / 240.0;
    if (dtSeconds <= 0.0 || dtSeconds > 0.1) {
        dtSeconds = 1.0 / 240.0;
    }
    const qreal cutoffHz = interpolate(40.0, 8.0, smoothing);
    const qreal alpha = 1.0 - std::exp(-2.0 * M_PI * cutoffHz * dtSeconds);
    m_assistedPressure = interpolate(m_assistedPressure, pressure, alpha);
    m_assistedPressureTimeMs = timeMs;
    return m_assistedPressure;
}

void KisToolSuperstrikeInk::beginPrimaryAction(KoPointerEvent *event)
{
    m_events.clear();
    m_rawPoints.clear();
    m_pathAssistInitialized = false;
    if (m_inkMode == InkMode::PerfectInk) {
        rememberEvent(event);
    }

    if (m_inkMode == InkMode::PathAssist) {
        const KoPointerEventWrapper original = event->deepCopyEvent();
        const auto *tablet = dynamic_cast<const QTabletEvent *>(
            original.baseQtEvent.data());
        auto assistedTablet = copyTabletEventWithPressure(
            tablet, assistedPressure(event->pressure(), event->time(), true));
        if (assistedTablet) {
            KoPointerEvent assistedEvent(
                assistedTablet.get(), pathAssistedPosition(event, true));
            KisToolFreehand::beginPrimaryAction(&assistedEvent);
        } else {
            KoPointerEvent assistedEvent(
                event, pathAssistedPosition(event, true));
            KisToolFreehand::beginPrimaryAction(&assistedEvent);
        }
    } else {
        // Native Brush and the Perfect Ink preview receive Krita's original
        // event unchanged. In particular, do not remap or smooth pressure.
        KisToolFreehand::beginPrimaryAction(event);
    }
    if (mode() != KisTool::PAINT_MODE) {
        m_events.clear();
        m_rawPoints.clear();
    }
}

void KisToolSuperstrikeInk::continuePrimaryAction(KoPointerEvent *event)
{
    if (m_inkMode == InkMode::PerfectInk) {
        rememberEvent(event);
    }
    if (m_inkMode == InkMode::PathAssist) {
        const KoPointerEventWrapper original = event->deepCopyEvent();
        const auto *tablet = dynamic_cast<const QTabletEvent *>(
            original.baseQtEvent.data());
        auto assistedTablet = copyTabletEventWithPressure(
            tablet, assistedPressure(event->pressure(), event->time(), false));
        if (assistedTablet) {
            KoPointerEvent assistedEvent(
                assistedTablet.get(), pathAssistedPosition(event, false));
            KisToolFreehand::continuePrimaryAction(&assistedEvent);
        } else {
            KoPointerEvent assistedEvent(
                event, pathAssistedPosition(event, false));
            KisToolFreehand::continuePrimaryAction(&assistedEvent);
        }
    } else {
        KisToolFreehand::continuePrimaryAction(event);
    }
}

void KisToolSuperstrikeInk::replayRefinedStroke()
{
    if (m_events.empty() || m_rawPoints.isEmpty()) {
        return;
    }
    const QVector<QPointF> refined = SuperstrikeInkFilter::refine(
        m_rawPoints, m_finalAmount, m_finalPasses);
    const QVector<ReplaySample> replay = prepareReplaySamples(
        refined,
        m_events,
        m_adaptiveTails,
        m_maximumTailLengthPx);
    if (replay.isEmpty()) {
        return;
    }
    m_replayHelper->cancelPreview();

    for (int index = 0; index < replay.size(); ++index) {
        const ReplaySample &sample = replay[index];
        const KoPointerEventWrapper &left = m_events[size_t(sample.leftIndex)];
        const KoPointerEventWrapper &right = m_events[size_t(sample.rightIndex)];
        const auto *leftTablet = dynamic_cast<const QTabletEvent *>(
            left.baseQtEvent.data());
        const auto *rightTablet = dynamic_cast<const QTabletEvent *>(
            right.baseQtEvent.data());
        const QPointF documentPoint = documentPositionFromWidget(sample.point);

        if (leftTablet && rightTablet) {
            const QTabletEvent *metadata = sample.amount < 0.5
                ? leftTablet
                : rightTablet;
            QTabletEvent tabletEvent(
                index == 0 ? leftTablet->type() : QEvent::TabletMove,
                leftTablet->posF() +
                    (rightTablet->posF() - leftTablet->posF()) * sample.amount,
                leftTablet->globalPosF() +
                    (rightTablet->globalPosF() - leftTablet->globalPosF()) *
                        sample.amount,
                metadata->deviceType(),
                metadata->pointerType(),
                sample.pressure,
                qRound(interpolate(leftTablet->xTilt(),
                                   rightTablet->xTilt(),
                                   sample.amount)),
                qRound(interpolate(leftTablet->yTilt(),
                                   rightTablet->yTilt(),
                                   sample.amount)),
                interpolate(leftTablet->tangentialPressure(),
                            rightTablet->tangentialPressure(),
                            sample.amount),
                interpolate(leftTablet->rotation(),
                            rightTablet->rotation(),
                            sample.amount),
                qRound(interpolate(leftTablet->z(),
                                   rightTablet->z(),
                                   sample.amount)),
                metadata->modifiers(),
                metadata->uniqueId(),
                index == 0 ? leftTablet->button() : Qt::NoButton,
                metadata->buttons());
            tabletEvent.setTimestamp(sample.timeMs);
            KoPointerEvent replayEvent(&tabletEvent, documentPoint);
            if (index == 0) {
                initStroke(&replayEvent);
            } else {
                doStroke(&replayEvent);
            }
        } else {
            const int sourceIndex = sample.amount < 0.5
                ? sample.leftIndex
                : sample.rightIndex;
            KoPointerEvent replayEvent(
                &m_events[size_t(sourceIndex)].event, documentPoint);
            if (index == 0) {
                initStroke(&replayEvent);
            } else {
                doStroke(&replayEvent);
            }
        }
    }
}

void KisToolSuperstrikeInk::renderPerfectFreehandStroke()
{
    if (m_events.empty() || m_rawPoints.isEmpty()) {
        return;
    }
    const QVector<QPointF> refined = SuperstrikeInkFilter::refine(
        m_rawPoints, m_finalAmount, m_finalPasses);
    QVector<ReplaySample> replay = prepareReplaySamples(
        refined,
        m_events,
        false,
        m_maximumTailLengthPx);
    if (replay.isEmpty()) {
        return;
    }

    if (m_adaptiveTails && replay.size() >= 4) {
        const qreal totalLength = replay.back().distance;
        const DetectedTail outlineStartTail = detectTail(
            replay, totalLength, true, m_maximumTailLengthPx);
        const DetectedTail outlineEndTail = detectTail(
            replay, totalLength, false, m_maximumTailLengthPx);
        int firstIndex = 0;
        int lastIndex = replay.size() - 1;
        if (outlineStartTail.valid) {
            while (firstIndex < lastIndex &&
                   replay[firstIndex].distance < outlineStartTail.length) {
                ++firstIndex;
            }
        }
        if (outlineEndTail.valid) {
            const qreal cutoff = totalLength - outlineEndTail.length;
            while (lastIndex > firstIndex &&
                   replay[lastIndex].distance > cutoff) {
                --lastIndex;
            }
        }
        if (lastIndex > firstIndex) {
            replay = replay.mid(firstIndex, lastIndex - firstIndex + 1);
        }
    }

    QVector<PerfectFreehandSample> samples;
    samples.reserve(replay.size());
    for (const ReplaySample &sample : replay) {
        // KisFigurePaintingToolHelper forwards polygon vertices directly to
        // KisPainter, whose geometry is expressed in image pixels. The live
        // freehand path uses document coordinates, so it intentionally keeps
        // using documentPositionFromWidget() elsewhere.
        samples.push_back({imagePositionFromWidget(sample.point),
                           sample.pressure});
    }

    qreal brushDiameter = 16.0;
    const KisPaintOpPresetSP preset = currentPaintOpPreset();
    if (preset && preset->settings()) {
        brushDiameter = qMax<qreal>(0.1,
            preset->settings()->paintOpSize());
    }
    PerfectFreehandOptions options;
    options.thinning = m_perfectFreehandThinning;
    options.smoothing = m_perfectFreehandSmoothing;
    options.streamline = m_perfectFreehandStreamline;
    options.complete = true;
    // perfect-freehand's `size` is the diameter at 0.5 pressure. Krita's
    // brush size is its maximum diameter, so normalize the outline to keep
    // full pressure aligned with the size shown in Krita's toolbar.
    options.size = brushDiameter /
        qMax<qreal>(0.05, 1.0 + options.thinning);

    const QVector<QPointF> outline = PerfectFreehandOutline::getStroke(
        samples, options);
    if (outline.size() < 3) {
        replayRefinedStroke();
        return;
    }

    m_replayHelper->cancelPreview();
    KisFigurePaintingToolHelper helper(
        kundo2_i18n("Superstrike Perfect Freehand Stroke"),
        image(),
        currentNode(),
        canvas()->resourceManager(),
        KisToolShapeUtils::StrokeStyleNone,
        KisToolShapeUtils::FillStyleForegroundColor);
    // Perfect Freehand's outline points are intentionally sparse. Its
    // reference renderer joins them with quadratic curves through successive
    // midpoints; drawing straight polygon edges makes large strokes look
    // faceted and turns round endpoint caps into visible bevels.
    //
    // QPainterPath defaults to odd-even filling, which cuts holes where the
    // variable-width outline crosses itself (for example in a handwritten
    // loop). Use non-zero winding so overlapping parts remain solid ink.
    QPainterPath outlinePath;
    outlinePath.setFillRule(Qt::WindingFill);
    outlinePath.moveTo(outline.front());
    const QPointF firstMidpoint =
        (outline[1] + outline[2]) / 2.0;
    outlinePath.quadTo(outline[1], firstMidpoint);
    for (int index = 2; index < outline.size() - 1; ++index) {
        const QPointF midpoint =
            (outline[index] + outline[index + 1]) / 2.0;
        outlinePath.quadTo(outline[index], midpoint);
    }
    outlinePath.closeSubpath();
    helper.paintPainterPath(outlinePath);
}

void KisToolSuperstrikeInk::endPrimaryAction(KoPointerEvent *event)
{
    if (mode() != KisTool::PAINT_MODE) {
        m_events.clear();
        m_rawPoints.clear();
        return;
    }
    if (m_inkMode == InkMode::PerfectInk && m_rawPoints.size() >= 2) {
        renderPerfectFreehandStroke();
    }
    if (m_inkMode == InkMode::PathAssist) {
        KoPointerEvent assistedEvent(event, pathAssistedPosition(event, false));
        KisToolFreehand::endPrimaryAction(&assistedEvent);
    } else {
        KisToolFreehand::endPrimaryAction(event);
    }
    m_events.clear();
    m_rawPoints.clear();
}

QWidget *KisToolSuperstrikeInk::createOptionWidget()
{
    QWidget *optionsWidget = KisToolFreehand::createOptionWidget();
    optionsWidget->setObjectName(toolId() + " option widget");

    auto *modeCombo = new QComboBox(optionsWidget);
    modeCombo->addItem(i18n("Native brush"));
    modeCombo->addItem(i18n("Native brush + Ink Assist"));
    modeCombo->addItem(i18n("Experimental Perfect Ink"));
    modeCombo->setCurrentIndex(static_cast<int>(m_inkMode));
    modeCombo->setToolTip(i18n(
        "Native modes preserve the active Krita preset and its pressure "
        "curves. Perfect Ink replaces the stroke with a solid filled outline."));
    connect(modeCombo,
            SIGNAL(currentIndexChanged(int)),
            this,
            SLOT(setInkMode(int)));
    addOptionWidgetOption(modeCombo,
                          new QLabel(i18n("Rendering mode:"), optionsWidget));

    auto *pathStrengthLabel = new QLabel(
        i18n("Path smoothing:"), optionsWidget);
    auto *pathStrength = new KisDoubleSliderSpinBox(optionsWidget);
    pathStrength->setRange(0.0, 1.0, 2);
    pathStrength->setSingleStep(0.05);
    pathStrength->setValue(m_pathAssistStrength);
    pathStrength->setToolTip(i18n(
        "Applies mild causal streamlining to position only. Pressure and all "
        "other brush-preset inputs pass through unchanged."));
    connect(pathStrength,
            SIGNAL(valueChanged(qreal)),
            this,
            SLOT(setPathAssistStrength(qreal)));
    addOptionWidgetOption(pathStrength, pathStrengthLabel);

    auto *pressureSmoothingLabel = new QLabel(
        i18n("Pressure smoothing:"), optionsWidget);
    auto *pressureSmoothing = new KisDoubleSliderSpinBox(optionsWidget);
    pressureSmoothing->setRange(0.0, 1.0, 2);
    pressureSmoothing->setSingleStep(0.05);
    pressureSmoothing->setValue(m_pressureSmoothing);
    pressureSmoothing->setToolTip(i18n(
        "Reduces sensor jitter before Krita applies the active preset's own "
        "pressure curve. Higher values add a small amount of width latency."));
    connect(pressureSmoothing,
            SIGNAL(valueChanged(qreal)),
            this,
            SLOT(setPressureSmoothing(qreal)));
    addOptionWidgetOption(pressureSmoothing, pressureSmoothingLabel);

    auto *amountLabel = new QLabel(i18n("Final smoothing:"), optionsWidget);
    auto *amount = new KisDoubleSliderSpinBox(optionsWidget);
    amount->setRange(0.0, 1.0, 2);
    amount->setSingleStep(0.01);
    amount->setValue(m_finalAmount);
    connect(amount, SIGNAL(valueChanged(qreal)), this, SLOT(setFinalAmount(qreal)));
    addOptionWidgetOption(amount, amountLabel);

    auto *passesLabel = new QLabel(i18n("Final passes:"), optionsWidget);
    auto *passes = new KisDoubleSliderSpinBox(optionsWidget);
    passes->setRange(0.0, 5.0, 0);
    passes->setSingleStep(1.0);
    passes->setValue(m_finalPasses);
    connect(passes, SIGNAL(valueChanged(qreal)), this, SLOT(setFinalPasses(qreal)));
    addOptionWidgetOption(passes, passesLabel);

    auto *shortTailToggle = new QCheckBox(
        i18n("Clip low-pressure endpoint tails"),
        optionsWidget);
    shortTailToggle->setChecked(m_adaptiveTails);
    connect(shortTailToggle,
            SIGNAL(toggled(bool)),
            this,
            SLOT(setAdaptiveTails(bool)));
    addOptionWidgetOption(shortTailToggle,
                          new QLabel(i18n("Adaptive tails:"), optionsWidget));

    auto *tailLengthLabel = new QLabel(
        i18n("Maximum detected tail (screen px):"), optionsWidget);
    auto *tailLength = new KisDoubleSliderSpinBox(optionsWidget);
    tailLength->setRange(16.0, 128.0, 0);
    tailLength->setSingleStep(4.0);
    tailLength->setValue(m_maximumTailLengthPx);
    connect(tailLength,
            SIGNAL(valueChanged(qreal)),
            this,
            SLOT(setMaximumTailLength(qreal)));
    addOptionWidgetOption(tailLength, tailLengthLabel);

    auto *streamlineLabel = new QLabel(i18n("Streamline:"), optionsWidget);
    auto *streamline = new KisDoubleSliderSpinBox(optionsWidget);
    streamline->setRange(0.0, 1.0, 2);
    streamline->setSingleStep(0.01);
    streamline->setValue(m_perfectFreehandStreamline);
    connect(streamline,
            SIGNAL(valueChanged(qreal)),
            this,
            SLOT(setPerfectFreehandStreamline(qreal)));
    addOptionWidgetOption(streamline, streamlineLabel);

    auto *outlineSmoothingLabel = new QLabel(
        i18n("Outline smoothing:"), optionsWidget);
    auto *outlineSmoothing = new KisDoubleSliderSpinBox(optionsWidget);
    outlineSmoothing->setRange(0.0, 1.0, 2);
    outlineSmoothing->setSingleStep(0.01);
    outlineSmoothing->setValue(m_perfectFreehandSmoothing);
    connect(outlineSmoothing,
            SIGNAL(valueChanged(qreal)),
            this,
            SLOT(setPerfectFreehandSmoothing(qreal)));
    addOptionWidgetOption(outlineSmoothing, outlineSmoothingLabel);

    auto *thinningLabel = new QLabel(i18n("Pressure response:"), optionsWidget);
    auto *thinning = new KisDoubleSliderSpinBox(optionsWidget);
    thinning->setRange(0.0, 1.0, 2);
    thinning->setSingleStep(0.01);
    thinning->setValue(m_perfectFreehandThinning);
    thinning->setToolTip(i18n(
        "Controls how much pressure changes outline width. At 1.0, zero "
        "pressure produces zero width and full pressure matches the current "
        "Krita brush diameter."));
    connect(thinning,
            SIGNAL(valueChanged(qreal)),
            this,
            SLOT(setPerfectFreehandThinning(qreal)));
    addOptionWidgetOption(thinning, thinningLabel);

    const QList<QWidget *> perfectInkControls = {
        static_cast<QWidget *>(amount),
        static_cast<QWidget *>(passes),
        static_cast<QWidget *>(shortTailToggle),
        static_cast<QWidget *>(tailLength),
        static_cast<QWidget *>(streamline),
                             static_cast<QWidget *>(outlineSmoothing),
        static_cast<QWidget *>(thinning),
    };
    pathStrength->setEnabled(m_inkMode == InkMode::PathAssist);
    pressureSmoothing->setEnabled(m_inkMode == InkMode::PathAssist);
    for (QWidget *control : perfectInkControls) {
        control->setEnabled(m_inkMode == InkMode::PerfectInk);
    }
    connect(modeCombo,
            QOverload<int>::of(&QComboBox::currentIndexChanged),
            optionsWidget,
            [pathStrength, pressureSmoothing, perfectInkControls](int mode) {
                pathStrength->setEnabled(
                    mode == static_cast<int>(InkMode::PathAssist));
                pressureSmoothing->setEnabled(
                    mode == static_cast<int>(InkMode::PathAssist));
                for (QWidget *control : perfectInkControls) {
                    control->setEnabled(
                        mode == static_cast<int>(InkMode::PerfectInk));
                }
            });
    for (QLabel *label : {amountLabel,
                          passesLabel,
                          tailLengthLabel,
                          streamlineLabel,
                          outlineSmoothingLabel,
                          thinningLabel}) {
        connect(modeCombo,
                QOverload<int>::of(&QComboBox::currentIndexChanged),
                label,
                [label](int mode) {
                    label->setEnabled(
                        mode == static_cast<int>(InkMode::PerfectInk));
                });
        label->setEnabled(m_inkMode == InkMode::PerfectInk);
    }
    pathStrengthLabel->setEnabled(m_inkMode == InkMode::PathAssist);
    pressureSmoothingLabel->setEnabled(m_inkMode == InkMode::PathAssist);
    connect(modeCombo,
            QOverload<int>::of(&QComboBox::currentIndexChanged),
            pathStrengthLabel,
            [pathStrengthLabel](int mode) {
                pathStrengthLabel->setEnabled(
                    mode == static_cast<int>(InkMode::PathAssist));
            });
    connect(modeCombo,
            QOverload<int>::of(&QComboBox::currentIndexChanged),
            pressureSmoothingLabel,
            [pressureSmoothingLabel](int mode) {
                pressureSmoothingLabel->setEnabled(
                    mode == static_cast<int>(InkMode::PathAssist));
            });
    return optionsWidget;
}

void KisToolSuperstrikeInk::setInkMode(int mode)
{
    m_inkMode = static_cast<InkMode>(qBound(0, mode, 2));
    KSharedConfig::openConfig()->group(toolId()).writeEntry("inkMode", mode);
}

void KisToolSuperstrikeInk::setPathAssistStrength(qreal value)
{
    m_pathAssistStrength = qBound<qreal>(0.0, value, 1.0);
    KSharedConfig::openConfig()->group(toolId()).writeEntry(
        "pathAssistStrength", m_pathAssistStrength);
}

void KisToolSuperstrikeInk::setPressureSmoothing(qreal value)
{
    m_pressureSmoothing = qBound<qreal>(0.0, value, 1.0);
    KSharedConfig::openConfig()->group(toolId()).writeEntry(
        "pressureSmoothing", m_pressureSmoothing);
}

void KisToolSuperstrikeInk::setFinalAmount(qreal value)
{
    m_finalAmount = value;
    KSharedConfig::openConfig()->group(toolId()).writeEntry("finalAmount", value);
}

void KisToolSuperstrikeInk::setFinalPasses(qreal value)
{
    m_finalPasses = qRound(value);
    KSharedConfig::openConfig()->group(toolId()).writeEntry("finalPasses", m_finalPasses);
}

void KisToolSuperstrikeInk::setAdaptiveTails(bool enabled)
{
    m_adaptiveTails = enabled;
    KSharedConfig::openConfig()->group(toolId()).writeEntry(
        "adaptiveTails", enabled);
}

void KisToolSuperstrikeInk::setMaximumTailLength(qreal value)
{
    m_maximumTailLengthPx = qBound<qreal>(16.0, value, 128.0);
    KSharedConfig::openConfig()->group(toolId()).writeEntry(
        "maximumTailLengthPx", m_maximumTailLengthPx);
}

void KisToolSuperstrikeInk::setPerfectFreehandStreamline(qreal value)
{
    m_perfectFreehandStreamline = qBound<qreal>(0.0, value, 1.0);
    KSharedConfig::openConfig()->group(toolId()).writeEntry(
        "perfectFreehandStreamline", m_perfectFreehandStreamline);
}

void KisToolSuperstrikeInk::setPerfectFreehandSmoothing(qreal value)
{
    m_perfectFreehandSmoothing = qBound<qreal>(0.0, value, 1.0);
    KSharedConfig::openConfig()->group(toolId()).writeEntry(
        "perfectFreehandSmoothing", m_perfectFreehandSmoothing);
}

void KisToolSuperstrikeInk::setPerfectFreehandThinning(qreal value)
{
    m_perfectFreehandThinning = qBound<qreal>(0.0, value, 1.0);
    KSharedConfig::openConfig()->group(toolId()).writeEntry(
        "perfectFreehandThinning", m_perfectFreehandThinning);
}
