/*
 * SPDX-FileCopyrightText: 2026 Ben Klein and contributors
 * SPDX-License-Identifier: GPL-2.0-or-later
 *
 * Algorithm adapted from perfect-freehand by Stephen Ruiz Ltd (MIT).
 * See THIRD_PARTY_NOTICES.md.
 */

#ifndef PERFECT_FREEHAND_OUTLINE_H
#define PERFECT_FREEHAND_OUTLINE_H

#include <QPointF>
#include <QVector>

struct PerfectFreehandSample
{
    QPointF point;
    qreal pressure {0.5};
};

struct PerfectFreehandOptions
{
    qreal size {16.0};
    qreal thinning {0.5};
    qreal smoothing {0.5};
    qreal streamline {0.5};
    qreal startTaper {0.0};
    qreal endTaper {0.0};
    bool complete {true};
};

class PerfectFreehandOutline
{
public:
    static QVector<QPointF> getStroke(
        const QVector<PerfectFreehandSample> &samples,
        const PerfectFreehandOptions &options = {});
};

#endif
