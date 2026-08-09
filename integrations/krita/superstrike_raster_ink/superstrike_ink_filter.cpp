/*
 * SPDX-FileCopyrightText: 2026 Ben Klein and contributors
 * SPDX-License-Identifier: GPL-2.0-or-later
 */

#include "superstrike_ink_filter.h"

#include <algorithm>
#include <cmath>
#include <QtMath>

namespace {
qreal lerp(qreal a, qreal b, qreal amount)
{
    return a + (b - a) * amount;
}

QVector<qreal> cumulativeLengths(const QVector<QPointF> &points)
{
    QVector<qreal> distances;
    distances.reserve(points.size());
    distances.push_back(0.0);
    for (int index = 1; index < points.size(); ++index) {
        const QPointF delta = points[index] - points[index - 1];
        distances.push_back(distances.back() +
                            std::hypot(delta.x(), delta.y()));
    }
    return distances;
}

QPointF sampleAtDistance(const QVector<QPointF> &points,
                         const QVector<qreal> &distances,
                         qreal distance)
{
    if (distance <= 0.0) {
        return points.front();
    }
    if (distance >= distances.back()) {
        return points.back();
    }

    const auto upper = std::upper_bound(distances.cbegin(),
                                        distances.cend(),
                                        distance);
    const int right = int(std::distance(distances.cbegin(), upper));
    const int left = right - 1;
    const qreal span = distances[right] - distances[left];
    if (span <= 0.000001) {
        return points[right];
    }
    const qreal amount = (distance - distances[left]) / span;
    return points[left] + (points[right] - points[left]) * amount;
}

QPointF mirroredPoint(const QVector<QPointF> &points, int index)
{
    const int last = points.size() - 1;
    if (index < 0) {
        return 2.0 * points.front() - points[qMin(last, -index)];
    }
    if (index > last) {
        return 2.0 * points.back() - points[qMax(0, 2 * last - index)];
    }
    return points[index];
}

qreal cornerProtection(const QVector<QPointF> &points,
                       int index,
                       int radius)
{
    const int last = points.size() - 1;
    const int span = qMax(3, radius);
    const QPointF incoming = points[index] - points[qMax(0, index - span)];
    const QPointF outgoing = points[qMin(last, index + span)] - points[index];
    const qreal incomingLength = std::hypot(incoming.x(), incoming.y());
    const qreal outgoingLength = std::hypot(outgoing.x(), outgoing.y());
    if (incomingLength < 2.0 || outgoingLength < 2.0) {
        return 0.0;
    }

    const qreal cosine = QPointF::dotProduct(incoming, outgoing) /
        (incomingLength * outgoingLength);
    return qBound<qreal>(0.0, (0.45 - cosine) / 0.45, 1.0);
}

QVector<QPointF> smoothUniform(const QVector<QPointF> &points,
                               qreal amount,
                               int radius,
                               int passes)
{
    QVector<QPointF> refined = points;
    const int safeRadius = qMin(qMax(2, radius), points.size() - 1);

    for (int pass = 0; pass < passes; ++pass) {
        const QVector<QPointF> source = refined;
        for (int index = 1; index < source.size() - 1; ++index) {
            QPointF target;
            qreal weightSum = 0.0;
            for (int offset = -safeRadius; offset <= safeRadius; ++offset) {
                const qreal weight = safeRadius + 1 - qAbs(offset);
                target += mirroredPoint(source, index + offset) * weight;
                weightSum += weight;
            }
            target /= weightSum;
            const qreal blend = amount *
                (1.0 - cornerProtection(source, index, safeRadius));
            refined[index] = source[index] + (target - source[index]) * blend;
        }
        refined.front() = points.front();
        refined.back() = points.back();
    }
    return refined;
}
}

void SuperstrikeInkFilter::setMinCutoff(qreal value)
{
    m_minCutoffHz = qMax<qreal>(0.001, value);
}

void SuperstrikeInkFilter::setSpeedCoefficient(qreal value)
{
    m_speedCoefficient = qMax<qreal>(0.0, value);
}

void SuperstrikeInkFilter::reset()
{
    m_initialized = false;
    m_lastTimeMs = 0;
    m_filteredDerivative = QPointF();
}

qreal SuperstrikeInkFilter::alpha(qreal cutoffHz, qreal dtSeconds)
{
    const qreal safeCutoff = qMax<qreal>(0.001, cutoffHz);
    const qreal safeDt = qMax<qreal>(0.000001, dtSeconds);
    const qreal tau = 1.0 / (2.0 * M_PI * safeCutoff);
    return 1.0 / (1.0 + tau / safeDt);
}

QPointF SuperstrikeInkFilter::update(const QPointF &point, ulong timeMs)
{
    if (!m_initialized) {
        m_initialized = true;
        m_lastRaw = point;
        m_lastFiltered = point;
        m_lastTimeMs = timeMs;
        return point;
    }

    qreal dt = qreal(timeMs - m_lastTimeMs) / 1000.0;
    if (dt <= 0.0 || dt > 0.1) {
        dt = 1.0 / 240.0;
    }

    const QPointF rawDerivative = (point - m_lastRaw) / dt;
    const qreal derivativeAlpha = alpha(m_derivativeCutoffHz, dt);
    m_filteredDerivative = QPointF(
        lerp(m_filteredDerivative.x(), rawDerivative.x(), derivativeAlpha),
        lerp(m_filteredDerivative.y(), rawDerivative.y(), derivativeAlpha));
    const qreal speed = std::hypot(m_filteredDerivative.x(),
                                   m_filteredDerivative.y());
    const qreal cutoff = m_minCutoffHz + m_speedCoefficient * speed;
    const qreal positionAlpha = alpha(cutoff, dt);
    m_lastFiltered = QPointF(
        lerp(m_lastFiltered.x(), point.x(), positionAlpha),
        lerp(m_lastFiltered.y(), point.y(), positionAlpha));
    m_lastRaw = point;
    m_lastTimeMs = timeMs;
    return m_lastFiltered;
}

QVector<QPointF> SuperstrikeInkFilter::refine(const QVector<QPointF> &points,
                                              qreal amount,
                                              int passes)
{
    if (points.size() < 3) {
        return points;
    }

    const qreal safeAmount = qBound<qreal>(0.0, amount, 1.0);
    const int safePasses = qMax(0, passes);
    if (safeAmount <= 0.0 || safePasses == 0) {
        return points;
    }

    const QVector<qreal> originalDistances = cumulativeLengths(points);
    const qreal totalLength = originalDistances.back();
    if (totalLength < 2.0) {
        return points;
    }

    QVector<qreal> uniformDistances;
    uniformDistances.reserve(qCeil(totalLength) + 1);
    for (int distance = 0; distance <= qFloor(totalLength); ++distance) {
        uniformDistances.push_back(qreal(distance));
    }
    if (uniformDistances.isEmpty() ||
        qAbs(uniformDistances.back() - totalLength) > 0.000001) {
        uniformDistances.push_back(totalLength);
    }

    QVector<QPointF> uniformPoints;
    uniformPoints.reserve(uniformDistances.size());
    for (qreal distance : uniformDistances) {
        uniformPoints.push_back(sampleAtDistance(points,
                                                 originalDistances,
                                                 distance));
    }

    const int radius = qMax(2, qRound(2.0 + 8.0 * safeAmount));
    const QVector<QPointF> smoothed = smoothUniform(uniformPoints,
                                                   safeAmount,
                                                   radius,
                                                   safePasses);

    QVector<QPointF> refined;
    refined.reserve(points.size());
    for (qreal distance : originalDistances) {
        refined.push_back(sampleAtDistance(smoothed,
                                           uniformDistances,
                                           distance));
    }
    refined.front() = points.front();
    refined.back() = points.back();
    return refined;
}
