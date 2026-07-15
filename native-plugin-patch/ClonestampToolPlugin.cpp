/*
 *  SPDX-FileCopyrightText: 2026 metamountain <mail@metamountain.net>
 *  SPDX-License-Identifier: GPL-2.0-or-later
 */

#include "ClonestampToolPlugin.h"

#include <kpluginfactory.h>

#include <KoToolRegistry.h>

#include "KisToolCloneStamp.h"

K_PLUGIN_FACTORY_WITH_JSON(ClonestampToolPluginFactory, "kritatoolclonestamp.json", registerPlugin<ClonestampToolPlugin>();)

ClonestampToolPlugin::ClonestampToolPlugin(QObject *parent, const QVariantList &)
    : QObject(parent)
{
    KoToolRegistry::instance()->add(new KisToolCloneStampFactory());
}

ClonestampToolPlugin::~ClonestampToolPlugin()
{
}

#include "ClonestampToolPlugin.moc"
