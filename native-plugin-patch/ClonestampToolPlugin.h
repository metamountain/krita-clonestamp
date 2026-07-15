/*
 *  SPDX-FileCopyrightText: 2026 metamountain <mail@metamountain.net>
 *  SPDX-License-Identifier: GPL-2.0-or-later
 */

#ifndef CLONESTAMP_TOOL_PLUGIN_H_
#define CLONESTAMP_TOOL_PLUGIN_H_

#include <QObject>
#include <QVariant>

class ClonestampToolPlugin : public QObject
{
    Q_OBJECT
public:
    ClonestampToolPlugin(QObject *parent, const QVariantList &);
    ~ClonestampToolPlugin() override;
};

#endif // CLONESTAMP_TOOL_PLUGIN_H_
