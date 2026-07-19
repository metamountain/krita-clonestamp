# SPDX-License-Identifier: CC0-1.0
# SPDX-FileCopyrightText: 2026 metamountain <mail@metamountain.net>

# Plugin entry point Krita actually loads (per kritapykrita_clonestamp.desktop's
# X-KDE-Library=clonestamp). Registers the docker defined in
# clonestamp_docker.py as a dockable panel (Settings > Dockers in Krita) --
# that docker is where all of this plugin's actual logic lives; see its
# module docstring for the full feature map.

from krita import Krita, DockWidgetFactory, DockWidgetFactoryBase
from .clonestamp_docker import ClonestampDocker

Krita.instance().addDockWidgetFactory(
    DockWidgetFactory("clonestamp_docker", DockWidgetFactoryBase.DockPosition.DockRight,
                       ClonestampDocker))
