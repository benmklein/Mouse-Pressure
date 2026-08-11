/*
 * SPDX-FileCopyrightText: 2026 Ben Klein and contributors
 * SPDX-License-Identifier: GPL-2.0-or-later
 */

#ifndef TOOL_MOUSE_PRESSURE_H
#define TOOL_MOUSE_PRESSURE_H

#include <QObject>
#include <QVariant>

class ToolMousePressure : public QObject
{
    Q_OBJECT

public:
    ToolMousePressure(QObject *parent, const QVariantList &);
    ~ToolMousePressure() override;
};

#endif
