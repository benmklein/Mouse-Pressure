/*
 * SPDX-FileCopyrightText: 2026 Ben Klein and contributors
 * SPDX-License-Identifier: GPL-2.0-or-later
 */

#ifndef KIS_TOOL_MOUSE_PRESSURE_H
#define KIS_TOOL_MOUSE_PRESSURE_H

#include <QKeySequence>
#include <QVector>
#include <vector>

#include <KoPointerEvent.h>
#include <KisToolPaintFactoryBase.h>
#include <klocalizedstring.h>
#include <kis_icon.h>
#include <kis_tool_freehand.h>

#include "mouse_pressure_ink_filter.h"

class KisDoubleSliderSpinBox;
class QCheckBox;
class QComboBox;
class MousePressureReplayHelper;

class KisToolMousePressure : public KisToolFreehand
{
    Q_OBJECT

public:
    explicit KisToolMousePressure(KoCanvasBase *canvas);
    ~KisToolMousePressure() override;

    QWidget *createOptionWidget() override;
    void activate(const QSet<KoShape *> &shapes) override;
    void beginPrimaryAction(KoPointerEvent *event) override;
    void continuePrimaryAction(KoPointerEvent *event) override;
    void endPrimaryAction(KoPointerEvent *event) override;

private Q_SLOTS:
    void setInkMode(int mode);
    void setPathAssistStrength(qreal value);
    void setPressureSmoothing(qreal value);
    void setFinalAmount(qreal value);
    void setFinalPasses(qreal value);
    void setAdaptiveTails(bool enabled);
    void setMaximumTailLength(qreal value);
    void setPerfectFreehandStreamline(qreal value);
    void setPerfectFreehandSmoothing(qreal value);
    void setPerfectFreehandThinning(qreal value);

private:
    enum class InkMode {
        NativeBrush = 0,
        PathAssist = 1,
        PerfectInk = 2,
    };

    void rememberEvent(KoPointerEvent *event);
    void replayRefinedStroke();
    void renderPerfectFreehandStroke();
    QPointF pathAssistedPosition(KoPointerEvent *event, bool begin);
    qreal assistedPressure(qreal pressure, ulong timeMs, bool begin);
    qreal zoomAssistMultiplier() const;
    QPointF documentPositionFromWidget(const QPointF &point) const;
    QPointF imagePositionFromWidget(const QPointF &point) const;

    MousePressureReplayHelper *m_replayHelper {nullptr};
    std::vector<KoPointerEventWrapper> m_events;
    QVector<QPointF> m_rawPoints;
    InkMode m_inkMode {InkMode::NativeBrush};
    qreal m_pathAssistStrength {0.2};
    qreal m_pressureSmoothing {0.25};
    QPointF m_pathAssistPoint;
    bool m_pathAssistInitialized {false};
    qreal m_assistedPressure {0.0};
    ulong m_assistedPressureTimeMs {0};
    qreal m_finalAmount {0.0};
    qreal m_maximumTailLengthPx {72.0};
    qreal m_perfectFreehandStreamline {0.5};
    qreal m_perfectFreehandSmoothing {0.5};
    qreal m_perfectFreehandThinning {1.0};
    int m_finalPasses {0};
    bool m_adaptiveTails {true};
};

class KisToolMousePressureFactory : public KisToolPaintFactoryBase
{
public:
    KisToolMousePressureFactory()
        : KisToolPaintFactoryBase("KritaShape/KisToolMousePressure")
    {
        setToolTip(i18n("Mouse Pressure Brush Tool (Shift+B)"));
        setSection(ToolBoxSection::Shape);
        setIconName(koIconNameCStr("mouse_pressure_mouse"));
        setShortcut(QKeySequence(Qt::SHIFT | Qt::Key_B));
        setPriority(11);
        setActivationShapeId(KRITA_TOOL_ACTIVATION_ID);
    }

    KoToolBase *createTool(KoCanvasBase *canvas) override
    {
        return new KisToolMousePressure(canvas);
    }
};

#endif
