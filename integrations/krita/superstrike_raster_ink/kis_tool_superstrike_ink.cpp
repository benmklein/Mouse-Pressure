/*
 * SPDX-FileCopyrightText: 2026 Ben Klein and contributors
 * SPDX-License-Identifier: GPL-2.0-or-later
 */

#include "kis_tool_superstrike_ink.h"

#include <algorithm>
#include <cmath>
#include <QCheckBox>
#include <QLabel>
#include <QTabletEvent>
#include <QtMath>

#include <kconfiggroup.h>
#include <ksharedconfig.h>
#include <klocalizedstring.h>

#include <KoCanvasBase.h>
#include <canvas/kis_canvas2.h>
#include <canvas/kis_coordinates_converter.h>
#include <kis_cursor.h>
#include <kis_slider_spin_box.h>
#include <kis_smoothing_options.h>
#include <kis_tool_freehand_helper.h>
#include <kundo2magicstring.h>

namespace {
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
            return {true,
                    edgeDistance,
                    qMax(sample.pressure, bodyPressure * 0.8)};
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
            sample.pressure = startTail.bodyPressure *
                (0.06 + 0.94 * std::pow(progress, 0.55));
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
    m_minCutoffHz = config.readEntry("minCutoffHz", 18.0);
    m_speedCoefficient = config.readEntry("speedCoefficient", 0.08);
    m_liveSmoothing = config.readEntry("liveSmoothing", false);
    m_finalAmount = config.readEntry("finalAmount", 0.42);
    m_finalPasses = config.readEntry("finalPasses", 2);
    m_finalRefinement = config.readEntry("finalRefinement", true);
    m_adaptiveTails = config.readEntry("adaptiveTails", true);
    m_maximumTailLengthPx = config.readEntry("maximumTailLengthPx", 72.0);
    m_filter.setMinCutoff(m_minCutoffHz);
    m_filter.setSpeedCoefficient(m_speedCoefficient);
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

void KisToolSuperstrikeInk::beginPrimaryAction(KoPointerEvent *event)
{
    m_events.clear();
    m_rawPoints.clear();
    m_filter.reset();
    rememberEvent(event);
    const QPointF position = m_liveSmoothing
        ? documentPositionFromWidget(m_filter.update(
              convertDocumentToWidget(event->point), event->time()))
        : event->point;
    KoPointerEvent filteredEvent(event, position);
    KisToolFreehand::beginPrimaryAction(&filteredEvent);
    if (mode() != KisTool::PAINT_MODE) {
        m_events.clear();
        m_rawPoints.clear();
    }
}

void KisToolSuperstrikeInk::continuePrimaryAction(KoPointerEvent *event)
{
    rememberEvent(event);
    const QPointF position = m_liveSmoothing
        ? documentPositionFromWidget(m_filter.update(
              convertDocumentToWidget(event->point), event->time()))
        : event->point;
    KoPointerEvent filteredEvent(event, position);
    KisToolFreehand::continuePrimaryAction(&filteredEvent);
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

void KisToolSuperstrikeInk::endPrimaryAction(KoPointerEvent *event)
{
    if (mode() != KisTool::PAINT_MODE) {
        m_events.clear();
        m_rawPoints.clear();
        return;
    }
    if (m_finalRefinement && m_rawPoints.size() >= 3) {
        replayRefinedStroke();
    }
    KisToolFreehand::endPrimaryAction(event);
    m_events.clear();
    m_rawPoints.clear();
}

QWidget *KisToolSuperstrikeInk::createOptionWidget()
{
    QWidget *optionsWidget = KisToolFreehand::createOptionWidget();
    optionsWidget->setObjectName(toolId() + " option widget");

    auto *liveToggle = new QCheckBox(i18n("Smooth live preview (adds trailing)"), optionsWidget);
    liveToggle->setChecked(m_liveSmoothing);
    connect(liveToggle, SIGNAL(toggled(bool)), this, SLOT(setLiveSmoothing(bool)));
    addOptionWidgetOption(liveToggle, new QLabel(i18n("Live pass:"), optionsWidget));

    auto *cutoffLabel = new QLabel(i18n("Live smoothing cutoff:"), optionsWidget);
    auto *cutoff = new KisDoubleSliderSpinBox(optionsWidget);
    cutoff->setRange(1.0, 80.0, 1);
    cutoff->setSingleStep(1.0);
    cutoff->setValue(m_minCutoffHz);
    connect(cutoff, SIGNAL(valueChanged(qreal)), this, SLOT(setMinCutoff(qreal)));
    addOptionWidgetOption(cutoff, cutoffLabel);

    auto *speedLabel = new QLabel(i18n("Speed response:"), optionsWidget);
    auto *speed = new KisDoubleSliderSpinBox(optionsWidget);
    speed->setRange(0.0, 0.5, 3);
    speed->setSingleStep(0.01);
    speed->setValue(m_speedCoefficient);
    connect(speed, SIGNAL(valueChanged(qreal)), this, SLOT(setSpeedCoefficient(qreal)));
    addOptionWidgetOption(speed, speedLabel);

    auto *finalToggle = new QCheckBox(i18n("Refine after release"), optionsWidget);
    finalToggle->setChecked(m_finalRefinement);
    connect(finalToggle, SIGNAL(toggled(bool)), this, SLOT(setFinalRefinement(bool)));
    addOptionWidgetOption(finalToggle, new QLabel(i18n("Final pass:"), optionsWidget));

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
        i18n("Detect and shape endpoint pressure tails"),
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
    return optionsWidget;
}

void KisToolSuperstrikeInk::setMinCutoff(qreal value)
{
    m_minCutoffHz = value;
    m_filter.setMinCutoff(value);
    KSharedConfig::openConfig()->group(toolId()).writeEntry("minCutoffHz", value);
}

void KisToolSuperstrikeInk::setSpeedCoefficient(qreal value)
{
    m_speedCoefficient = value;
    m_filter.setSpeedCoefficient(value);
    KSharedConfig::openConfig()->group(toolId()).writeEntry("speedCoefficient", value);
}

void KisToolSuperstrikeInk::setLiveSmoothing(bool enabled)
{
    m_liveSmoothing = enabled;
    KSharedConfig::openConfig()->group(toolId()).writeEntry("liveSmoothing", enabled);
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

void KisToolSuperstrikeInk::setFinalRefinement(bool enabled)
{
    m_finalRefinement = enabled;
    KSharedConfig::openConfig()->group(toolId()).writeEntry("finalRefinement", enabled);
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
