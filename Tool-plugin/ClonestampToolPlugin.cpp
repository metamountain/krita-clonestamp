/*
 *  SPDX-FileCopyrightText: 2026 metamountain <mail@metamountain.net>
 *  SPDX-License-Identifier: GPL-2.0-or-later
 */

#include "ClonestampToolPlugin.h"

#include <kpluginfactory.h>

#include <KoToolManager.h>
#include <KoToolManager_p.h>
#include <KoToolRegistry.h>

#include "KisToolCloneStamp.h"

namespace
{
const QString TOOL_ID = QStringLiteral("KritaShape/KisToolCloneStamp");

// Registers the tool factory once, however the DLL got loaded.
void registerCloneStampTool()
{
    KoToolRegistry *registry = KoToolRegistry::instance();
    if (!registry->contains(TOOL_ID)) {
        registry->add(new KisToolCloneStampFactory());
    }
}

// KoToolManager builds its tool action list once, from the registry, when it
// is first used -- before Python plugins run. A tool registered later (our
// ctypes loader) is selectable via its action but gets no toolbox button
// unless its action is added to that list before the toolbox is built.
// Same approach as Acly's krita-vision-tools (injectTools). Guarded so a
// second call or an already-listed tool never adds a duplicate.
void injectToolboxAction()
{
    KoToolManager::Private *p = KoToolManager::instance()->priv();
    for (KoToolAction *action : std::as_const(p->toolActionList)) {
        if (action->id() == TOOL_ID) {
            return;
        }
    }
    if (KoToolFactoryBase *factory = KoToolRegistry::instance()->value(TOOL_ID)) {
        p->toolActionList.append(new KoToolAction(factory));
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

// Entry point for the plugin-zip distribution: the clonestamp_tool Python
// loader loads this DLL with ctypes and calls this function -- no admin
// rights, nothing written into the Krita install.
extern "C" Q_DECL_EXPORT void load_clonestamp_plugin()
{
    registerCloneStampTool();
    injectToolboxAction();
}

#include "ClonestampToolPlugin.moc"
