/*
 * SPDX-FileCopyrightText: 2026 Ben Klein and contributors
 * SPDX-License-Identifier: GPL-2.0-or-later
 *
 * Algorithm adapted from perfect-freehand by Stephen Ruiz Ltd (MIT).
 * See THIRD_PARTY_NOTICES.md.
 */

#include "perfect_freehand_outline.h"

#include <algorithm>
#include <cmath>
#include <QtMath>

namespace {
constexpr qreal MIN_STREAMLINE_T = 0.15;
constexpr qreal STREAMLINE_T_RANGE = 0.85;
constexpr qreal END_NOISE_THRESHOLD = 3.0;
constexpr qreal MIN_RADIUS = 0.01;
constexpr int START_CAP_SEGMENTS = 13;
constexpr int END_CAP_SEGMENTS = 29;
constexpr int CORNER_CAP_SEGMENTS = 13;
constexpr qreal FIXED_PI = M_PI + 0.0001;

struct StrokePoint
{
    QPointF point;
    qreal pressure {0.5};
    QPointF vector;
    qreal distance {0.0};
    qreal runningLength {0.0};
};

qreal length(const QPointF &vector)
{
    return std::hypot(vector.x(), vector.y());
}

QPointF unit(const QPointF &vector)
{
    const qreal magnitude = length(vector);
    return magnitude <= 0.000001 ? QPointF() : vector / magnitude;
}

QPointF perpendicular(const QPointF &vector)
{
    return QPointF(vector.y(), -vector.x());
}

QPointF interpolate(const QPointF &left,
                    const QPointF &right,
                    qreal amount)
{
    return left + (right - left) * amount;
}

qreal interpolate(qreal left, qreal right, qreal amount)
{
    return left + (right - left) * amount;
}

QPointF rotateAround(const QPointF &point,
                     const QPointF &center,
                     qreal radians)
{
    const qreal sine = std::sin(radians);
    const qreal cosine = std::cos(radians);
    const QPointF local = point - center;
    return center + QPointF(local.x() * cosine - local.y() * sine,
                            local.x() * sine + local.y() * cosine);
}

qreal squaredDistance(const QPointF &left, const QPointF &right)
{
    const QPointF delta = left - right;
    return delta.x() * delta.x() + delta.y() * delta.y();
}

qreal strokeRadius(qreal size, qreal thinning, qreal pressure)
{
    return size * (0.5 - thinning * (0.5 - pressure));
}

QVector<StrokePoint> getStrokePoints(
    const QVector<PerfectFreehandSample> &input,
    const PerfectFreehandOptions &options)
{
    if (input.isEmpty()) {
        return {};
    }

    QVector<PerfectFreehandSample> samples = input;
    if (samples.size() == 2) {
        const PerfectFreehandSample first = samples.front();
        const PerfectFreehandSample last = samples.back();
        samples.clear();
        samples.push_back(first);
        for (int index = 1; index < 5; ++index) {
            const qreal amount = qreal(index) / 4.0;
            samples.push_back({interpolate(first.point, last.point, amount),
                               interpolate(first.pressure,
                                           last.pressure,
                                           amount)});
        }
    } else if (samples.size() == 1) {
        samples.push_back({samples.front().point + QPointF(1.0, 1.0),
                           samples.front().pressure});
    }

    const qreal streamline = qBound<qreal>(0.0, options.streamline, 1.0);
    const qreal amount = MIN_STREAMLINE_T +
        (1.0 - streamline) * STREAMLINE_T_RANGE;
    QVector<StrokePoint> points;
    points.reserve(samples.size());
    points.push_back({samples.front().point,
                      qBound<qreal>(0.0, samples.front().pressure, 1.0),
                      QPointF(1.0, 1.0),
                      0.0,
                      0.0});

    bool reachedMinimumLength = false;
    qreal runningLength = 0.0;
    const int lastIndex = samples.size() - 1;
    for (int index = 1; index < samples.size(); ++index) {
        StrokePoint &previous = points.back();
        const QPointF point = options.complete && index == lastIndex
            ? samples[index].point
            : interpolate(previous.point, samples[index].point, amount);
        if (squaredDistance(point, previous.point) <= 0.0000001) {
            continue;
        }
        const qreal distance = length(point - previous.point);
        runningLength += distance;
        if (index < lastIndex && !reachedMinimumLength) {
            if (runningLength < qMax<qreal>(0.01, options.size)) {
                continue;
            }
            reachedMinimumLength = true;
        }
        points.push_back({point,
                          qBound<qreal>(0.0, samples[index].pressure, 1.0),
                          unit(previous.point - point),
                          distance,
                          runningLength});
    }
    points.front().vector = points.size() > 1
        ? points[1].vector
        : QPointF();
    return points;
}

QVector<QPointF> drawDot(const QPointF &center, qreal radius)
{
    const QPointF offsetPoint = center + QPointF(1.0, 1.0);
    const QPointF start = center -
        unit(perpendicular(center - offsetPoint)) * radius;
    QVector<QPointF> points;
    points.reserve(START_CAP_SEGMENTS);
    for (int index = 1; index <= START_CAP_SEGMENTS; ++index) {
        points.push_back(rotateAround(
            start,
            center,
            FIXED_PI * 2.0 * qreal(index) / START_CAP_SEGMENTS));
    }
    return points;
}

QVector<QPointF> getOutline(const QVector<StrokePoint> &points,
                            const PerfectFreehandOptions &options)
{
    if (points.isEmpty() || options.size <= 0.0) {
        return {};
    }
    const qreal totalLength = points.back().runningLength;
    const qreal taperStart = qMax<qreal>(0.0, options.startTaper);
    const qreal taperEnd = qMax<qreal>(0.0, options.endTaper);
    const qreal minDistanceSquared = std::pow(
        options.size * qBound<qreal>(0.0, options.smoothing, 1.0), 2.0);

    qreal previousPressure = points.front().pressure;
    const int pressureCount = qMin(10, points.size());
    for (int index = 0; index < pressureCount; ++index) {
        previousPressure = (previousPressure + points[index].pressure) / 2.0;
    }
    Q_UNUSED(previousPressure);

    qreal radius = strokeRadius(options.size,
                                options.thinning,
                                points.back().pressure);
    qreal firstRadius = radius;
    bool firstRadiusSet = false;
    QPointF previousVector = points.front().vector;
    QPointF previousLeft = points.front().point;
    QPointF previousRight = previousLeft;
    bool previousSharp = false;
    QVector<QPointF> leftPoints;
    QVector<QPointF> rightPoints;

    for (int index = 0; index < points.size(); ++index) {
        const StrokePoint &strokePoint = points[index];
        const bool last = index == points.size() - 1;
        if (!last && totalLength - strokePoint.runningLength <
                END_NOISE_THRESHOLD) {
            continue;
        }

        radius = options.thinning == 0.0
            ? options.size / 2.0
            : strokeRadius(options.size,
                           options.thinning,
                           strokePoint.pressure);
        if (!firstRadiusSet) {
            firstRadius = radius;
            firstRadiusSet = true;
        }
        const qreal startStrength = taperStart > 0.0 &&
                strokePoint.runningLength < taperStart
            ? 1.0 - std::pow(1.0 - strokePoint.runningLength / taperStart, 2.0)
            : 1.0;
        const qreal remaining = totalLength - strokePoint.runningLength;
        const qreal endStrength = taperEnd > 0.0 && remaining < taperEnd
            ? 1.0 - std::pow(1.0 - remaining / taperEnd, 3.0)
            : 1.0;
        radius = qMax(MIN_RADIUS,
                      radius * qMin(startStrength, endStrength));

        const QPointF nextVector = last
            ? strokePoint.vector
            : points[index + 1].vector;
        const qreal nextDot = QPointF::dotProduct(strokePoint.vector,
                                                  nextVector);
        const qreal previousDot = QPointF::dotProduct(strokePoint.vector,
                                                      previousVector);
        const bool sharp = previousDot < 0.0 && !previousSharp;
        const bool nextSharp = !last && nextDot < 0.0;
        if (sharp || nextSharp) {
            const QPointF offset = perpendicular(previousVector) * radius;
            for (int segment = 0; segment <= CORNER_CAP_SEGMENTS; ++segment) {
                const qreal turn = FIXED_PI * qreal(segment) /
                    CORNER_CAP_SEGMENTS;
                previousLeft = rotateAround(strokePoint.point - offset,
                                            strokePoint.point,
                                            turn);
                previousRight = rotateAround(strokePoint.point + offset,
                                             strokePoint.point,
                                             -turn);
                leftPoints.push_back(previousLeft);
                rightPoints.push_back(previousRight);
            }
            previousSharp = nextSharp;
            continue;
        }
        previousSharp = false;

        if (last) {
            const QPointF offset = perpendicular(strokePoint.vector) * radius;
            leftPoints.push_back(strokePoint.point - offset);
            rightPoints.push_back(strokePoint.point + offset);
            continue;
        }

        const QPointF blendedVector = interpolate(nextVector,
                                                  strokePoint.vector,
                                                  nextDot);
        const QPointF offset = perpendicular(blendedVector) * radius;
        const QPointF left = strokePoint.point - offset;
        const QPointF right = strokePoint.point + offset;
        if (index <= 1 || squaredDistance(previousLeft, left) >
                minDistanceSquared) {
            leftPoints.push_back(left);
            previousLeft = left;
        }
        if (index <= 1 || squaredDistance(previousRight, right) >
                minDistanceSquared) {
            rightPoints.push_back(right);
            previousRight = right;
        }
        previousPressure = strokePoint.pressure;
        previousVector = strokePoint.vector;
    }

    if (leftPoints.isEmpty() || rightPoints.isEmpty()) {
        return drawDot(points.front().point, firstRadius);
    }
    if (points.size() == 1) {
        return drawDot(points.front().point, firstRadius);
    }

    QVector<QPointF> endCap;
    QVector<QPointF> startCap;
    const QPointF firstPoint = points.front().point;
    const QPointF lastPoint = points.back().point;
    if (taperStart <= 0.0) {
        const QPointF right = rightPoints.front();
        for (int index = 1; index <= START_CAP_SEGMENTS; ++index) {
            startCap.push_back(rotateAround(
                right,
                firstPoint,
                FIXED_PI * qreal(index) / START_CAP_SEGMENTS));
        }
    }
    if (taperEnd > 0.0) {
        endCap.push_back(lastPoint);
    } else {
        const QPointF direction = perpendicular(-points.back().vector);
        const QPointF start = lastPoint + direction * radius;
        for (int index = 1; index < END_CAP_SEGMENTS; ++index) {
            endCap.push_back(rotateAround(
                start,
                lastPoint,
                FIXED_PI * 3.0 * qreal(index) / END_CAP_SEGMENTS));
        }
    }

    QVector<QPointF> outline = leftPoints;
    outline += endCap;
    std::reverse(rightPoints.begin(), rightPoints.end());
    outline += rightPoints;
    outline += startCap;
    return outline;
}
}

QVector<QPointF> PerfectFreehandOutline::getStroke(
    const QVector<PerfectFreehandSample> &samples,
    const PerfectFreehandOptions &options)
{
    PerfectFreehandOptions safe = options;
    safe.size = qMax<qreal>(0.01, safe.size);
    safe.thinning = qBound<qreal>(-1.0, safe.thinning, 1.0);
    safe.smoothing = qBound<qreal>(0.0, safe.smoothing, 1.0);
    safe.streamline = qBound<qreal>(0.0, safe.streamline, 1.0);
    return getOutline(getStrokePoints(samples, safe), safe);
}
