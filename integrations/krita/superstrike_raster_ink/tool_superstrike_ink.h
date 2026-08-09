/*
 * SPDX-FileCopyrightText: 2026 Ben Klein and contributors
 * SPDX-License-Identifier: GPL-2.0-or-later
 */

#ifndef TOOL_SUPERSTRIKE_INK_H
#define TOOL_SUPERSTRIKE_INK_H

#include <QObject>
#include <QVariant>

class ToolSuperstrikeInk : public QObject
{
    Q_OBJECT

public:
    ToolSuperstrikeInk(QObject *parent, const QVariantList &);
    ~ToolSuperstrikeInk() override;
};

#endif
