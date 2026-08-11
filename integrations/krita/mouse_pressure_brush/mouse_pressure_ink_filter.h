/*
 * SPDX-FileCopyrightText: 2026 Ben Klein and contributors
 * SPDX-License-Identifier: GPL-2.0-or-later
 */

#ifndef MOUSE_PRESSURE_INK_FILTER_H
#define MOUSE_PRESSURE_INK_FILTER_H

#include <QPointF>
#include <QVector>

class MousePressureInkFilter
{
public:
    void setMinCutoff(qreal value);
    void setSpeedCoefficient(qreal value);
    void reset();
    QPointF update(const QPointF &point, ulong timeMs);

    static QVector<QPointF> refine(const QVector<QPointF> &points,
                                   qreal amount,
                                   int passes);

private:
    static qreal alpha(qreal cutoffHz, qreal dtSeconds);

    bool m_initialized {false};
    QPointF m_lastRaw;
    QPointF m_lastFiltered;
    QPointF m_filteredDerivative;
    ulong m_lastTimeMs {0};
    qreal m_minCutoffHz {18.0};
    qreal m_speedCoefficient {0.08};
    qreal m_derivativeCutoffHz {1.0};
};

#endif
