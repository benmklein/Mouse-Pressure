/*
 * SPDX-FileCopyrightText: 2026 Ben Klein and contributors
 * SPDX-License-Identifier: GPL-2.0-or-later
 */

#include "tool_superstrike_ink.h"

#include <KoToolRegistry.h>
#include <kpluginfactory.h>

#include "kis_tool_superstrike_ink.h"

K_PLUGIN_FACTORY_WITH_JSON(ToolSuperstrikeInkFactory,
                           "kritatoolsuperstrikeink.json",
                           registerPlugin<ToolSuperstrikeInk>();)

ToolSuperstrikeInk::ToolSuperstrikeInk(QObject *parent, const QVariantList &)
    : QObject(parent)
{
    KoToolRegistry::instance()->add(new KisToolSuperstrikeInkFactory());
}

ToolSuperstrikeInk::~ToolSuperstrikeInk() = default;

#include "tool_superstrike_ink.moc"
