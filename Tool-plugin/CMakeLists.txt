set(kritatoolclonestamp_static_SOURCES
    KisToolCloneStamp.cpp
    )

set(kritatoolclonestamp_SOURCES
    ClonestampToolPlugin.cpp
    )

kis_add_library(kritatoolclonestamp_static STATIC ${kritatoolclonestamp_static_SOURCES})
generate_export_header(kritatoolclonestamp_static BASE_NAME kritatoolclonestamp)

kis_add_library(kritatoolclonestamp MODULE ${kritatoolclonestamp_SOURCES})

target_link_libraries(kritatoolclonestamp_static PUBLIC kritaui)
target_link_libraries(kritatoolclonestamp PRIVATE kritatoolclonestamp_static)

install(TARGETS kritatoolclonestamp DESTINATION ${KRITA_PLUGIN_INSTALL_DIR})
