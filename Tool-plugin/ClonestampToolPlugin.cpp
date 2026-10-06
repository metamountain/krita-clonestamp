/*
 *  SPDX-FileCopyrightText: 2026 metamountain <mail@metamountain.net>
 *  SPDX-License-Identifier: GPL-2.0-or-later
 */

#include "ClonestampToolPlugin.h"

#include <kpluginfactory.h>

#include <KoToolRegistry.h>

#include "KisToolCloneStamp.h"

namespace
{
// Registers the tool once, however the DLL got loaded: as a regular plugin
// from lib/kritaplugins (install.cmd) and/or through the Python loader below.
void registerCloneStampTool()
{
    KoToolRegistry *registry = KoToolRegistry::instance();
    if (!registry->contains(QStringLiteral("KritaShape/KisToolCloneStamp"))) {
        registry->add(new KisToolCloneStampFactory());
    }
}
} // namespace

K_PLUGIN_FACTORY_WITH_JSON(ClonestampToolPluginFactory, "kritatoolclonestamp.json", registerPlugin<ClonestampToolPlugin>();)

ClonestampToolPlugin::ClonestampToolPlugin(QObject *parent, const QVariantList &)
    : QObject(parent)
{
    registerCloneStampTool();
}

ClonestampToolPlugin::~ClonestampToolPlugin()
{
}

// Entry point for the Python-plugin distribution (same model as Acly's
// krita-ai-tools): a tiny pykrita plugin loads this DLL from its own folder
// with ctypes and calls this function -- no copy into Program Files, no
// admin rights, installed via Tools > Scripts > Import Python Plugin.
extern "C" Q_DECL_EXPORT void load_clonestamp_plugin()
{
    registerCloneStampTool();
}

#include "ClonestampToolPlugin.moc"
