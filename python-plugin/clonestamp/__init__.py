# SPDX-License-Identifier: CC0-1.0
# SPDX-FileCopyrightText: 2026 metamountain <mail@metamountain.net>

from krita import Krita, DockWidgetFactory, DockWidgetFactoryBase
from .clonestamp_docker import ClonestampDocker

Krita.instance().addDockWidgetFactory(
    DockWidgetFactory("clonestamp_docker", DockWidgetFactoryBase.DockPosition.DockRight,
                       ClonestampDocker))
