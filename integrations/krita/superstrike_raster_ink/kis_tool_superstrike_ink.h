/*
 * SPDX-FileCopyrightText: 2026 Ben Klein and contributors
 * SPDX-License-Identifier: GPL-2.0-or-later
 */

#ifndef KIS_TOOL_SUPERSTRIKE_INK_H
#define KIS_TOOL_SUPERSTRIKE_INK_H

#include <QKeySequence>
#include <QVector>
#include <optional>
#include <vector>

#include <KoPointerEvent.h>
#include <KisToolPaintFactoryBase.h>
#include <klocalizedstring.h>
#include <kis_icon.h>
#include <kis_tool_freehand.h>

#include "superstrike_ink_filter.h"

class KisDoubleSliderSpinBox;
class QCheckBox;
class SuperstrikeReplayHelper;

class KisToolSuperstrikeInk : public KisToolFreehand
{
    Q_OBJECT

public:
    explicit KisToolSuperstrikeInk(KoCanvasBase *canvas);
    ~KisToolSuperstrikeInk() override;

    QWidget *createOptionWidget() override;
    void activate(const QSet<KoShape *> &shapes) override;
    void beginPrimaryAction(KoPointerEvent *event) override;
    void continuePrimaryAction(KoPointerEvent *event) override;
    void endPrimaryAction(KoPointerEvent *event) override;

private Q_SLOTS:
    void setMinCutoff(qreal value);
    void setSpeedCoefficient(qreal value);
    void setLiveSmoothing(bool enabled);
    void setFinalAmount(qreal value);
    void setFinalPasses(qreal value);
    void setFinalRefinement(bool enabled);
    void setAdaptiveTails(bool enabled);
    void setMaximumTailLength(qreal value);

private:
    void rememberEvent(KoPointerEvent *event);
    void replayRefinedStroke();
    QPointF documentPositionFromWidget(const QPointF &point) const;

    SuperstrikeInkFilter m_filter;
    SuperstrikeReplayHelper *m_replayHelper {nullptr};
    std::vector<KoPointerEventWrapper> m_events;
    QVector<QPointF> m_rawPoints;
    qreal m_minCutoffHz {18.0};
    qreal m_speedCoefficient {0.08};
    qreal m_finalAmount {0.42};
    qreal m_maximumTailLengthPx {72.0};
    int m_finalPasses {2};
    bool m_liveSmoothing {false};
    bool m_finalRefinement {true};
    bool m_adaptiveTails {true};
};

class KisToolSuperstrikeInkFactory : public KisToolPaintFactoryBase
{
public:
    KisToolSuperstrikeInkFactory()
        : KisToolPaintFactoryBase("KritaShape/KisToolSuperstrikeInk")
    {
        setToolTip(i18n("Superstrike Raster Ink Tool"));
        setSection(ToolBoxSection::Shape);
        setIconName(koIconNameCStr("superstrike_mouse"));
        setShortcut(QKeySequence(Qt::SHIFT | Qt::Key_B));
        setPriority(11);
        setActivationShapeId(KRITA_TOOL_ACTIVATION_ID);
    }

    KoToolBase *createTool(KoCanvasBase *canvas) override
    {
        return new KisToolSuperstrikeInk(canvas);
    }
};

#endif
