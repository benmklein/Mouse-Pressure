/*
 * SPDX-FileCopyrightText: 2026 Ben Klein and contributors
 * SPDX-License-Identifier: GPL-2.0-or-later
 */

#include "tool_mouse_pressure.h"

#include <KoToolRegistry.h>
#include <kpluginfactory.h>

#include "kis_tool_mouse_pressure.h"

K_PLUGIN_FACTORY_WITH_JSON(ToolMousePressureFactory,
                           "kritatoolsmousepressure.json",
                           registerPlugin<ToolMousePressure>();)

ToolMousePressure::ToolMousePressure(QObject *parent, const QVariantList &)
    : QObject(parent)
{
    KoToolRegistry::instance()->add(new KisToolMousePressureFactory());
}

ToolMousePressure::~ToolMousePressure() = default;

#include "tool_mouse_pressure.moc"
